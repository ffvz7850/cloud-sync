# -*- coding: utf-8 -*-
"""下载链路测试：openlist 的流式下载 / 批量删除，以及 DownloadQueue 的状态机。

下载是「会把数据写到用户磁盘上」的那一半，比上传更怕写坏文件，所以这里重点
钉死三件事：

1. **写临时文件再改名** —— 中断不能留下半个文件冒充完整文件；
2. **续传要看服务端脸色** —— 带了 Range 却回 200 时必须清空重写，
   否则会把整份内容追加到半截文件后面，得到一个"大小对不上"的坏文件；
3. **大小必须核对** —— 不一致要报失败，而不是"看起来成功了"。
"""
import json
import os
import tempfile
import unittest

import support  # noqa: F401

import config
import download_queue
import httpclient
import openlist
import upload_queue


# ---------------------------------------------------------------------------
# 假传输层：能流式吐字节，也能回 206 / 401
# ---------------------------------------------------------------------------

class StreamResp(object):
    def __init__(self, status, chunks, headers=None):
        self.status = status
        self._chunks = list(chunks)
        self._pos = 0
        self.headers = dict(headers or {})
        self.closed = False

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    def iter_chunks(self, chunk_size=256 * 1024):
        while self._pos < len(self._chunks):
            chunk = self._chunks[self._pos]
            self._pos += 1
            yield chunk

    def read_all(self):
        return b"".join(self.iter_chunks())

    def read(self, n=-1):
        return self.read_all()

    def close(self):
        self.closed = True


class JsonResp(object):
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

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
        self.fallbacks = {}

    def push(self, method, url_sub, response):
        self.routes.setdefault((method, url_sub), []).append(response)
        return self

    def fallback(self, method, url_sub, fn):
        self.fallbacks[(method, url_sub)] = fn
        return self

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        for (m, sub), responses in self.routes.items():
            if m == method and sub in url and responses:
                return responses.pop(0)
        for (m, sub), fn in self.fallbacks.items():
            if m == method and sub in url:
                return fn(url, dict(headers or {}), body)
        return JsonResp(404, {"code": 404, "message": "not found:" + url, "data": None})


class PatchTransport(object):
    def __init__(self, transport):
        self.transport = transport
        self.orig = None

    def __enter__(self):
        self.orig = httpclient.request
        httpclient.request = self.transport
        return self.transport

    def __exit__(self, *a):
        httpclient.request = self.orig


def make_client(base="http://nas:5244"):
    support.temp_data_dir()
    path = os.path.join(os.environ["TRIM_PKGVAR"], "cred.json")
    store = openlist.CredentialStore(path)
    store.save({"baseUrl": base, "username": "alice",
                "passwordHash": openlist.static_hash("secret"), "token": "TOK-1"})
    return openlist.OpenlistClient(store), store


def get_resp(path, size=0, raw_url="", is_dir=False):
    node = {"name": path.rsplit("/", 1)[-1], "is_dir": is_dir, "size": size}
    if raw_url:
        node["raw_url"] = raw_url
    return JsonResp(200, {"code": 200, "message": "success", "data": node})


class AbsoluteUrlTest(unittest.TestCase):
    def test_relative_uses_openlist_base(self):
        client, _ = make_client()
        self.assertEqual(client.absolute_url("/d/a.bin?sign=x"),
                         "http://nas:5244/d/a.bin?sign=x")

    def test_absolute_passthrough(self):
        client, _ = make_client()
        self.assertEqual(client.absolute_url("https://cdn.example.com/a.bin"),
                         "https://cdn.example.com/a.bin")

    def test_protocol_relative(self):
        client, _ = make_client()
        self.assertEqual(client.absolute_url("//cdn/a.bin"), "http://cdn/a.bin")

    def test_empty(self):
        client, _ = make_client()
        self.assertEqual(client.absolute_url(""), "")


class SameOriginTest(unittest.TestCase):
    def test_matching_host_and_port(self):
        client, _ = make_client()
        self.assertTrue(client._same_origin("http://nas:5244/d/a"))

    def test_default_ports_match(self):
        client, _ = make_client("http://nas")
        self.assertTrue(client._same_origin("http://nas/d/a"))
        self.assertTrue(client._same_origin("https://nas:443/d/a") is False)

    def test_other_host(self):
        client, _ = make_client()
        self.assertFalse(client._same_origin("http://other:5244/d/a"))
        self.assertFalse(client._same_origin("https://cdn.example.com/a"))

    def test_other_port(self):
        client, _ = make_client()
        self.assertFalse(client._same_origin("http://nas:9999/d/a"))


class DownloadToFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dd-dl-")
        self.local = os.path.join(self.dir, "sub", "a.bin")

    # 刻意不做 teardown 清理：本沙箱里删除文件要走安全护栏，一次 unlink 要
    # 3 秒多，几十个用例下来光是删临时目录就能把测试拖到一分钟以上。
    # 项目里其它测试（test_verify / test_upload_queue）同样不清理临时目录。

    def test_happy_path_writes_and_renames(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=10,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin",
                       StreamResp(200, [b"hello", b"world"],
                                  {"content-length": "10"}))
        seen = []
        with PatchTransport(transport):
            result = client.download_to_file("/a.bin", self.local,
                                             on_progress=lambda got, total: seen.append((got, total)))

        self.assertEqual(result["size"], 10)
        with open(self.local, "rb") as fh:
            self.assertEqual(fh.read(), b"helloworld")
        # 临时文件必须被清掉（否则下次比对会把它当成"本地多出的文件"）
        self.assertFalse(os.path.exists(self.local + ".ddpart"))
        self.assertEqual(seen, [(5, 10), (10, 10)])
        # 父目录是自动建的
        self.assertTrue(os.path.isdir(os.path.dirname(self.local)))

    def test_same_origin_sends_token(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=3,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin", StreamResp(200, [b"abc"], {"content-length": "3"}))
        with PatchTransport(transport):
            client.download_to_file("/a.bin", self.local)
        get_call = [c for c in transport.calls if c["method"] == "GET"][0]
        self.assertEqual(get_call["headers"].get("Authorization"), "TOK-1")

    def test_cross_origin_does_not_leak_token(self):
        """第三方直链绝不能带本站 token —— 那是一次凭据泄露。"""
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=3,
                                                       raw_url="https://cdn.example.com/a.bin"))
        transport.push("GET", "cdn.example.com",
                       StreamResp(200, [b"abc"], {"content-length": "3"}))
        with PatchTransport(transport):
            client.download_to_file("/a.bin", self.local)
        get_call = [c for c in transport.calls if c["method"] == "GET"][0]
        self.assertNotIn("Authorization", get_call["headers"])

    def test_resume_appends_on_206(self):
        client, _ = make_client()
        os.makedirs(os.path.dirname(self.local), exist_ok=True)
        with open(self.local + ".ddpart", "wb") as fh:
            fh.write(b"hello")
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=10,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin",
                       StreamResp(206, [b"world"], {"content-length": "5"}))
        with PatchTransport(transport):
            result = client.download_to_file("/a.bin", self.local)
        get_call = [c for c in transport.calls if c["method"] == "GET"][0]
        self.assertEqual(get_call["headers"].get("Range"), "bytes=5-")
        self.assertEqual(result["size"], 10)
        with open(self.local, "rb") as fh:
            self.assertEqual(fh.read(), b"helloworld")

    def test_resume_restarts_when_server_ignores_range(self):
        """带了 Range 但服务端回 200（全量）→ 必须清空重写。

        否则就是"半截文件 + 完整内容"拼在一起，得到一个大小翻倍的坏文件。
        """
        client, _ = make_client()
        os.makedirs(os.path.dirname(self.local), exist_ok=True)
        with open(self.local + ".ddpart", "wb") as fh:
            fh.write(b"hello")
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=10,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin",
                       StreamResp(200, [b"helloworld"], {"content-length": "10"}))
        with PatchTransport(transport):
            result = client.download_to_file("/a.bin", self.local)
        self.assertEqual(result["size"], 10)
        with open(self.local, "rb") as fh:
            self.assertEqual(fh.read(), b"helloworld")

    def test_size_mismatch_raises_and_keeps_target_clean(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=10,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin",
                       StreamResp(200, [b"short"], {"content-length": "5"}))
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.download_to_file("/a.bin", self.local)
        self.assertIn("不完整", str(cm.exception))
        # 目标路径上不该出现"貌似成功"的半截文件
        self.assertFalse(os.path.exists(self.local))

    def test_expected_size_from_compare_is_honoured(self):
        """云端条目没给 size 时，用比对结果里的大小兜底校验。"""
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=0,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin", StreamResp(200, [b"abc"]))
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError):
                client.download_to_file("/a.bin", self.local, expected_size=99)

    def test_cancel_keeps_part_file(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=10,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin",
                       StreamResp(200, [b"hello", b"world"], {"content-length": "10"}))
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 1        # 第一块之后取消

        with PatchTransport(transport):
            with self.assertRaises(openlist.UploadCancelled):
                client.download_to_file("/a.bin", self.local, should_cancel=cancel)
        # 已下载的字节留着，下次从断点继续
        self.assertTrue(os.path.exists(self.local + ".ddpart"))
        self.assertFalse(os.path.exists(self.local))
        with open(self.local + ".ddpart", "rb") as fh:
            self.assertEqual(fh.read(), b"hello")

    def test_missing_raw_url_reports_clearly(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=3))
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.download_to_file("/a.bin", self.local)
        self.assertIn("下载地址", str(cm.exception))

    def test_directory_rejected(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get",
                       get_resp("/dir", size=0, raw_url="/d/dir", is_dir=True))
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.download_to_file("/dir", self.local)
        self.assertIn("目录", str(cm.exception))

    def test_401_triggers_relogin_then_succeeds(self):
        client, _ = make_client()
        transport = FakeTransport()
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=3,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin", JsonResp(401, {"code": 401, "message": "unauthorized"}))
        # 重登
        transport.push("POST", "/api/auth/login/hash",
                       JsonResp(200, {"code": 200, "data": {"token": "TOK-2"}}))
        # 重登后重新取 raw_url 再下
        transport.push("POST", "/api/fs/get", get_resp("/a.bin", size=3,
                                                       raw_url="/d/a.bin"))
        transport.push("GET", "/d/a.bin", StreamResp(200, [b"abc"], {"content-length": "3"}))
        with PatchTransport(transport):
            result = client.download_to_file("/a.bin", self.local)
        self.assertEqual(result["size"], 3)
        self.assertEqual(client.store.token(), "TOK-2")


class RemoveManyTest(unittest.TestCase):
    def _client(self, transport):
        client, _ = make_client()
        patch = PatchTransport(transport)
        patch.__enter__()
        self.addCleanup(patch.__exit__, None, None, None)
        return client

    def test_groups_by_parent_dir(self):
        transport = FakeTransport()
        transport.push("POST", "/api/fs/remove", JsonResp(200, {"code": 200, "data": None}))
        transport.push("POST", "/api/fs/remove", JsonResp(200, {"code": 200, "data": None}))
        client = self._client(transport)
        removed, failed = client.remove_many(["/a/1.txt", "/a/2.txt", "/b/3.txt"])
        self.assertEqual(sorted(removed), ["/a/1.txt", "/a/2.txt", "/b/3.txt"])
        self.assertEqual(failed, [])
        # 两个目录 → 两次请求（不是三条一次）
        self.assertEqual(len([c for c in transport.calls if c["method"] == "POST"]), 2)
        bodies = [json.loads(c["body"].decode("utf-8")) for c in transport.calls]
        a_call = [b for b in bodies if b["dir"] == "/a"][0]
        self.assertEqual(sorted(a_call["names"]), ["1.txt", "2.txt"])

    def test_group_failure_falls_back_to_one_by_one(self):
        transport = FakeTransport()
        transport.push("POST", "/api/fs/remove",
                       JsonResp(403, {"code": 403, "message": "permission denied"}))
        transport.push("POST", "/api/fs/remove", JsonResp(200, {"code": 200, "data": None}))
        transport.push("POST", "/api/fs/remove",
                       JsonResp(404, {"code": 404, "message": "object not found"}))
        client = self._client(transport)
        removed, failed = client.remove_many(["/a/1.txt", "/a/2.txt"])
        self.assertEqual(removed, ["/a/1.txt"])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["path"], "/a/2.txt")
        self.assertIn("not found", failed[0]["error"])

    def test_root_refused(self):
        transport = FakeTransport()
        transport.push("POST", "/api/fs/remove", JsonResp(200, {"code": 200, "data": None}))
        client = self._client(transport)
        removed, failed = client.remove_many(["/", "/a/1.txt"])
        # 根目录被拒，其余条目照常处理 —— 一条坏输入不该拖垮整批
        self.assertEqual(removed, ["/a/1.txt"])
        self.assertEqual(len(failed), 1)
        self.assertIn("根目录", failed[0]["error"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(json.loads(transport.calls[0]["body"].decode("utf-8"))["dir"], "/a")


# ---------------------------------------------------------------------------
# DownloadQueue
# ---------------------------------------------------------------------------

class FakeDownloadClient(object):
    def __init__(self):
        self.remote = {}          # remotePath -> bytes
        self.fail_times = 0
        self.fail_with = None
        self.calls = []

    def download_to_file(self, remote, local, on_progress=None,
                         should_cancel=None, expected_size=None):
        self.calls.append({"remote": remote, "local": local,
                           "expected": expected_size})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with or openlist.NetworkError("模拟网络抖动")
        if remote not in self.remote:
            raise openlist.OpenlistError("云端找不到：%s" % remote)
        data = self.remote[remote]
        if expected_size and len(data) != expected_size:
            raise openlist.OpenlistError("下载不完整（%s）" % remote)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(data)
        if on_progress:
            on_progress(len(data) // 2, len(data))
            on_progress(len(data), len(data))
        return {"size": len(data), "path": local}


def make_queue(client=None, max_attempts=3):
    support.temp_data_dir()
    path = os.path.join(os.environ["TRIM_PKGVAR"], "dq.json")
    return download_queue.DownloadQueue(client or FakeDownloadClient(), path=path,
                                        max_attempts=max_attempts, base_delay=0.01)


class DownloadQueueAddTest(unittest.TestCase):
    def test_marks_direction_and_name(self):
        q = make_queue()
        tasks = q.add([{"remotePath": "/a/b.mkv", "localPath": "/tmp/x/other.mkv",
                        "size": 123}])
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["direction"], "download")
        self.assertEqual(task["name"], "b.mkv")       # 用云端名，不是本地落点名
        self.assertEqual(task["size"], 123)
        self.assertEqual(task["status"], upload_queue.PENDING)
        self.assertTrue(task["overwrite"])

    def test_accepts_nonexistent_local_target(self):
        """下载任务的本地文件本来就还不存在 —— 不能像上传那样把它丢掉。"""
        q = make_queue()
        tasks = q.add([{"remotePath": "/a/b.mkv",
                        "localPath": "/definitely/not/here/b.mkv", "size": 9}])
        self.assertEqual(len(tasks), 1)

    def test_skips_incomplete_items(self):
        q = make_queue()
        tasks = q.add([{"remotePath": "", "localPath": "/x"},
                       {"remotePath": "/y", "localPath": ""},
                       "not-a-dict"])
        self.assertEqual(tasks, [])

    def test_persisted_with_direction(self):
        q = make_queue()
        q.add([{"remotePath": "/a/b.mkv", "localPath": "/tmp/b.mkv", "size": 1}])
        raw = json.loads(open(q.path, encoding="utf-8").read())
        self.assertEqual(raw["tasks"][0]["direction"], "download")


class DownloadQueueRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dd-dq-")

    # 刻意不做 teardown 清理：本沙箱里删除文件要走安全护栏，一次 unlink 要
    # 3 秒多，几十个用例下来光是删临时目录就能把测试拖到一分钟以上。
    # 项目里其它测试（test_verify / test_upload_queue）同样不清理临时目录。

    def test_run_task_creates_parent_and_finishes(self):
        client = FakeDownloadClient()
        client.remote["/src/a.bin"] = b"0123456789"
        q = make_queue(client)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "deep", "a.bin"),
                       "size": 10}])[0]
        q._run_task(task)
        done = q.get(task["id"])
        self.assertEqual(done["status"], upload_queue.DONE)
        self.assertEqual(done["uploaded"], 10)
        self.assertEqual(done["size"], 10)
        with open(os.path.join(self.dir, "deep", "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"0123456789")

    def test_run_task_retries_then_succeeds(self):
        client = FakeDownloadClient()
        client.remote["/src/a.bin"] = b"xy"
        client.fail_times = 1
        q = make_queue(client)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "a.bin"),
                       "size": 2}])[0]
        q._run_task(task)          # 第一次失败 → 退避 → 回 pending
        self.assertEqual(q.get(task["id"])["status"], upload_queue.PENDING)
        q._run_task(q.get(task["id"]))
        self.assertEqual(q.get(task["id"])["status"], upload_queue.DONE)

    def test_run_task_gives_up_after_max_attempts(self):
        client = FakeDownloadClient()
        client.fail_with = openlist.OpenlistError("云端找不到：/src/a.bin")
        q = make_queue(client, max_attempts=1)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "a.bin"),
                       "size": 2}])[0]
        q._run_task(task)
        failed = q.get(task["id"])
        self.assertEqual(failed["status"], upload_queue.FAILED)
        self.assertIn("云端找不到", failed["error"])

    def test_auth_error_is_not_retried(self):
        # 登录态失效且重登失败：重试多少次都是白搭，必须直接判失败让用户去重登
        client = FakeDownloadClient()
        client.fail_times = 9
        client.fail_with = openlist.AuthError("登录态失效")
        q = make_queue(client, max_attempts=5)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "a.bin"),
                       "size": 2}])[0]
        q._run_task(task)
        self.assertEqual(q.get(task["id"])["status"], upload_queue.FAILED)

    def test_expected_size_passed_from_task(self):
        client = FakeDownloadClient()
        client.remote["/src/a.bin"] = b"abc"
        q = make_queue(client)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "a.bin"),
                       "size": 3}])[0]
        q._run_task(task)
        self.assertEqual(client.calls[-1]["expected"], 3)

    def test_worker_loop_end_to_end(self):
        client = FakeDownloadClient()
        client.remote["/src/a.bin"] = b"abc"
        q = make_queue(client)
        q.start(workers=1)
        try:
            task = q.add([{"remotePath": "/src/a.bin",
                           "localPath": os.path.join(self.dir, "a.bin"),
                           "size": 3}])[0]
            import time
            for _ in range(200):
                if q.get(task["id"])["status"] in upload_queue.TERMINAL:
                    break
                time.sleep(0.01)
            self.assertEqual(q.get(task["id"])["status"], upload_queue.DONE)
        finally:
            q.stop()

    def test_resume_resets_inflight_to_pending(self):
        client = FakeDownloadClient()
        q = make_queue(client)
        task = q.add([{"remotePath": "/src/a.bin",
                       "localPath": os.path.join(self.dir, "a.bin"),
                       "size": 3}])[0]
        with q._lock:
            for t in q._tasks:
                if t["id"] == task["id"]:
                    t["status"] = upload_queue.UPLOADING
                    t["uploaded"] = 1
        q.save(force=True)
        q2 = download_queue.DownloadQueue(client, path=q.path)
        self.assertEqual(q2.get(task["id"])["status"], upload_queue.PENDING)
        self.assertEqual(q2.get(task["id"])["direction"], "download")


class QueueSeparationTest(unittest.TestCase):
    """上传与下载必须是两条独立队列，各自有独立的数据文件。"""

    def test_files_are_different(self):
        support.temp_data_dir()
        path = download_queue.DownloadQueue(FakeDownloadClient()).path
        up = upload_queue.UploadQueue(FakeDownloadClient(),
                                      path=os.path.join(config.data_dir(),
                                                        "upload_queue.json")).path
        self.assertNotEqual(path, up)
        self.assertTrue(path.endswith("download_queue.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
