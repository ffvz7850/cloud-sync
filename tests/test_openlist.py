# -*- coding: utf-8 -*-
"""openlist 客户端契约测试。用假的传输层钉死 OpenList 的调用形态。

重点验证：登录请求体、token 用法（裸值 header）、401 自动重登、File-Path 的
percent-encode、multipart snapshot 的闭区间解析、大小校验。这些都是「写错一个
细节就连不上」的地方。
"""
import json
import os
import tempfile
import unittest

import support  # noqa: F401

import httpclient
import openlist


class FakeResp(object):
    def __init__(self, status, payload):
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read_all(self):
        return self._raw

    def read(self, n=-1):
        return self._raw

    def close(self):
        pass


class FakeTransport(object):
    """按 (method, url 子串) 匹配，按序返回预设响应。"""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def push(self, method, url_sub, payload, status=200):
        self.routes.setdefault((method, url_sub), []).append((status, payload))
        return self

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        for (m, sub), responses in self.routes.items():
            if m == method and sub in url and responses:
                status, payload = responses.pop(0)
                return FakeResp(status, payload)
        return FakeResp(404, {"code": 404, "message": "not found:" + url, "data": None})

    def last(self):
        return self.calls[-1]


def make_client(transport, with_token=True):
    support.temp_data_dir()
    path = os.path.join(os.environ["TRIM_PKGVAR"], "cred.json")
    store = openlist.CredentialStore(path)
    if with_token:
        store.save({"baseUrl": "http://nas:5244", "username": "alice",
                    "passwordHash": openlist.static_hash("secret"), "token": "TOK-1"})
    client = openlist.OpenlistClient(store)
    return client, store


class PatchTransport:
    """with 块内替换 httpclient.request。"""

    def __init__(self, transport):
        self.transport = transport
        self.orig = None

    def __enter__(self):
        self.orig = httpclient.request
        httpclient.request = self.transport
        return self.transport

    def __exit__(self, *a):
        httpclient.request = self.orig


class NormalizeTest(unittest.TestCase):
    def test_various_forms(self):
        self.assertEqual(openlist.normalize_base_url("192.168.0.10:5244"),
                         "http://192.168.0.10:5244")
        self.assertEqual(openlist.normalize_base_url("http://a:5244/"), "http://a:5244")
        self.assertEqual(openlist.normalize_base_url("http://a:5244/api"), "http://a:5244")
        self.assertEqual(openlist.normalize_base_url("https://nas.local"), "https://nas.local")
        self.assertEqual(openlist.normalize_base_url("  http://b:1/  "), "http://b:1")


class LoginTest(unittest.TestCase):
    def test_login_body_and_store(self):
        transport = FakeTransport()
        transport.push("POST", "/api/auth/login/hash",
                       {"code": 200, "message": "success", "data": {"token": "TOK-NEW"}})
        client, store = make_client(transport, with_token=False)
        with PatchTransport(transport):
            client.login("http://nas:5244/", "alice", "secret")

        call = transport.last()
        # 登录请求必须打到完整 URL（首次登录时 store 里还没有地址，
        # 历史上这里拼出过相对路径 "/api/auth/login/hash" → "unknown url type"）
        self.assertEqual(call["url"], "http://nas:5244/api/auth/login/hash")
        body = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(body["username"], "alice")
        self.assertEqual(body["password"], openlist.static_hash("secret"))
        # 登录请求不应带 Authorization
        self.assertNotIn("Authorization", call["headers"])
        # 凭据落盘：存的是 StaticHash 而不是明文，token 也存了
        self.assertEqual(store.token(), "TOK-NEW")
        self.assertEqual(store.password_hash(), openlist.static_hash("secret"))
        self.assertNotIn("secret", json.dumps(store.load()))

    def test_login_failure_raises_auth(self):
        transport = FakeTransport()
        transport.push("POST", "/api/auth/login/hash",
                       {"code": 401, "message": "Invalid username or password", "data": None},
                       status=401)
        client, store = make_client(transport, with_token=False)
        with PatchTransport(transport):
            with self.assertRaises(openlist.AuthError):
                client.login("http://nas:5244", "alice", "wrong")


class UnconfiguredTest(unittest.TestCase):
    def test_request_without_base_url_gives_friendly_error(self):
        """没配地址就调业务接口，必须报「尚未配置」而不是 "unknown url type"。"""
        transport = FakeTransport()
        client, store = make_client(transport, with_token=False)
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.list_dir("/")
        self.assertIn("尚未配置", str(cm.exception))
        self.assertEqual(transport.calls, [])  # 不该真发请求


class TokenTest(unittest.TestCase):
    def test_token_sent_as_bare_authorization(self):
        transport = FakeTransport()
        transport.push("POST", "/api/fs/list",
                       {"code": 200, "data": {"content": [], "total": 0, "write": True}})
        client, store = make_client(transport)
        with PatchTransport(transport):
            client.list_dir("/")
        # Authorization 是裸 token，没有 Bearer 前缀
        self.assertEqual(transport.last()["headers"].get("Authorization"), "TOK-1")

    def test_401_triggers_relogin_and_retry(self):
        transport = FakeTransport()
        # 第一次 list 401，重登，第二次 list 200
        transport.push("POST", "/api/fs/list",
                       {"code": 401, "message": "expired", "data": None}, status=401)
        transport.push("POST", "/api/auth/login/hash",
                       {"code": 200, "data": {"token": "TOK-2"}})
        transport.push("POST", "/api/fs/list",
                       {"code": 200, "data": {"content": [{"name": "x", "is_dir": False,
                                                           "size": 1}], "total": 1}})
        client, store = make_client(transport)
        with PatchTransport(transport):
            data = client.list_dir("/")
        self.assertEqual(data["total"], 1)
        self.assertEqual(store.token(), "TOK-2")
        # 重试的那次 list 带的是新 token
        list_calls = [c for c in transport.calls if "/api/fs/list" in c["url"]]
        self.assertEqual(list_calls[-1]["headers"].get("Authorization"), "TOK-2")


class FakeListTransport(object):
    """**真按 page/per_page 切片**返回的假服务端。

    必须真切片，不能像 FakeTransport 那样「按序丢预置响应」：那样分页逻辑写错
    （翻页起点算错、per_page 用错）也会看起来通过。`per_page_cap` 用来模拟
    服务端把 per_page 夹到自身上限 —— 这时切片用的是**夹后**的值。
    """

    def __init__(self, items, total=None, per_page_cap=None):
        self.items = list(items)
        self.total = len(self.items) if total is None else total
        self.per_page_cap = per_page_cap
        self.calls = []
        self.per_pages = []          # 每次请求带上来的 per_page（看自适应有没有生效）
        self.page_nums = []

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        if "/api/fs/list" not in url:
            return FakeResp(404, {"code": 404, "message": "not found:" + url, "data": None})
        req = json.loads(body.decode("utf-8"))
        page = int(req.get("page") or 1)
        per_page = int(req.get("per_page") or 0)
        self.per_pages.append(per_page)
        self.page_nums.append(page)
        if self.per_page_cap:
            per_page = min(per_page, self.per_page_cap)
        start = (page - 1) * per_page
        chunk = self.items[start:start + per_page]
        return FakeResp(200, {"code": 200, "message": "success",
                              "data": {"content": chunk, "total": self.total,
                                       "write": True, "provider": "fake"}})


class ListAllTest(unittest.TestCase):
    """翻页 + 自适应分页。条数必须完整、不重不漏，且请求次数要压得住。"""

    def _items(self, n):
        return [{"name": "f%06d" % i, "is_dir": False, "size": 1} for i in range(n)]

    def _list(self, transport):
        client, _ = make_client(transport)
        with PatchTransport(transport):
            return client.list_all("/")

    def _assert_complete(self, out, n):
        names = [x["name"] for x in out]
        self.assertEqual(len(names), n, "条数不符")
        self.assertEqual(len(set(names)), n, "出现重复条目（页码推进算错了）")
        self.assertEqual(names, sorted(names), "顺序应保持服务端返回序")

    def test_small_dir_one_request(self):
        transport = FakeListTransport(self._items(10))
        out = self._list(transport)
        self._assert_complete(out, 10)
        self.assertEqual(len(transport.calls), 1)

    def test_exactly_one_page_one_request(self):
        transport = FakeListTransport(self._items(1000))
        out = self._list(transport)
        self._assert_complete(out, 1000)
        self.assertEqual(len(transport.calls), 1,
                         "刚好一页不该多打一次空请求，也不该触发自适应")

    def test_just_over_one_page_adapts_to_two_requests(self):
        transport = FakeListTransport(self._items(1001))
        out = self._list(transport)
        self._assert_complete(out, 1001)
        self.assertEqual(transport.per_pages, [1000, 1001],
                         "第一页探到 total 后应改用 total 一次拿全")
        self.assertEqual(len(transport.calls), 2)

    def test_large_dir_two_requests_instead_of_pages(self):
        transport = FakeListTransport(self._items(8000))
        out = self._list(transport)
        self._assert_complete(out, 8000)
        self.assertEqual(len(transport.calls), 2,
                         "8000 项：原来要 8 次往返，自适应后只该 2 次")

    def test_over_max_per_page_falls_back_to_plain_paging(self):
        n = 12000          # > MAX_PER_PAGE，不启用自适应
        transport = FakeListTransport(self._items(n))
        out = self._list(transport)
        self._assert_complete(out, n)
        self.assertTrue(all(p == openlist.DEFAULT_PER_PAGE for p in transport.per_pages),
                        "超上限不该把 per_page 顶到 %d" % n)
        self.assertEqual(len(transport.calls), n // openlist.DEFAULT_PER_PAGE)

    def test_server_capping_per_page_still_returns_everything(self):
        """服务端把 per_page 夹到自身上限时，也**绝不能漏条目**。

        漏条目会被判成「云端没有」→ 已经传好的文件被重复上传。
        这里服务端一律只给 2000 条，翻页起点按夹后的 2000 算。
        """
        n = 5000
        transport = FakeListTransport(self._items(n), per_page_cap=2000)
        out = self._list(transport)
        self._assert_complete(out, n)
        self.assertEqual(transport.per_pages[0], openlist.DEFAULT_PER_PAGE)

    def test_respects_larger_total_than_items(self):
        """total 比实际条目多（服务端还在写入）时不得死循环，也不能重复条目。"""
        transport = FakeListTransport(self._items(300), total=99999)
        with PatchTransport(transport):
            client, _ = make_client(transport)
            out = client.list_all("/")
        self.assertEqual(len(out), 300)
        self.assertEqual(len(set(x["name"] for x in out)), 300)
        self.assertLess(len(transport.calls), 20, "不该无限翻页")


class EnsureDirTest(unittest.TestCase):
    def test_creates_parents(self):
        transport = FakeTransport()
        # stat（fs/get）对 /a 和 /a/b 都返回 404，mkdir 两次成功
        transport.push("POST", "/api/fs/get", {"code": 404, "message": "not found"}, status=404)
        transport.push("POST", "/api/fs/get", {"code": 404, "message": "not found"}, status=404)
        transport.push("POST", "/api/fs/mkdir", {"code": 200, "data": None})
        transport.push("POST", "/api/fs/mkdir", {"code": 200, "data": None})
        client, _ = make_client(transport)
        with PatchTransport(transport):
            client.ensure_dir("/a/b")
        mkdir_calls = [c for c in transport.calls if "/api/fs/mkdir" in c["url"]]
        paths = [json.loads(c["body"].decode())["path"] for c in mkdir_calls]
        self.assertEqual(paths, ["/a", "/a/b"])


class SnapshotTest(unittest.TestCase):
    def test_closed_interval_received(self):
        """received 是闭区间 [[start,end]]，展开成已传分片集合。"""
        snap = openlist.OpenlistClient._normalize_snapshot({
            "upload_id": "u", "chunk_size": 10, "total_chunks": 5,
            "received": [[0, 1], [3, 3]], "received_bytes": 30, "frontier": 2,
            "resumed": True})
        self.assertEqual(snap["done_chunks"], {0, 1, 3})
        self.assertEqual(snap["chunk_size"], 10)
        self.assertEqual(snap["total_chunks"], 5)
        self.assertTrue(snap["resumed"])
        # 待传 = total - done = {2, 4}
        self.assertEqual(set(range(5)) - snap["done_chunks"], {2, 4})


class UploadTest(unittest.TestCase):
    def _patch_put(self):
        captured = {}

        class PutPatcher:
            def __enter__(self2):
                self2.orig = httpclient.put_stream

                def fake(url, headers, body, size, on_progress=None, timeout=None, **kw):
                    captured["url"] = url
                    captured["headers"] = dict(headers)
                    captured["size"] = size
                    if on_progress:
                        on_progress(size, size)
                    return FakeResp(200, {"code": 200, "message": "success", "data": None})

                httpclient.put_stream = fake
                return captured

            def __exit__(self2, *a):
                httpclient.put_stream = self2.orig
        return PutPatcher(), captured

    def test_upload_file_sets_file_path_header(self):
        # fs/get 用于 verify，返回匹配大小
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get",
                       {"code": 200, "data": {"name": "报告 v1.txt", "is_dir": False, "size": 5}})
        client, store = make_client(transport)

        fd, local = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"hello")
        os.close(fd)
        try:
            patcher, captured = self._patch_put()
            with patcher, PatchTransport(transport):
                client.upload_file(local, "/docs/报告 v1.txt", verify=True)
            # File-Path 必须 percent-encode（中文、空格）
            fp = captured["headers"]["File-Path"]
            self.assertNotIn("报告", fp)
            self.assertNotIn(" ", fp)
            self.assertIn("%E6%8A%A5%E5%91%8A", fp)   # 报告 的 UTF-8 编码
            self.assertEqual(captured["headers"]["Authorization"], "TOK-1")
            self.assertEqual(captured["size"], 5)
        finally:
            os.unlink(local)

    def test_verify_size_mismatch_raises(self):
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get",
                       {"code": 200, "data": {"name": "a", "is_dir": False, "size": 999}})
        client, store = make_client(transport)

        fd, local = tempfile.mkstemp()
        os.write(fd, b"hello")
        os.close(fd)
        try:
            patcher, captured = self._patch_put()
            with patcher, PatchTransport(transport):
                with self.assertRaises(openlist.OpenlistError) as cm:
                    client.upload_file(local, "/a", verify=True)
            self.assertIn("大小不一致", str(cm.exception))
        finally:
            os.unlink(local)


if __name__ == "__main__":
    unittest.main(verbosity=2)
