# -*- coding: utf-8 -*-
"""2.15.48 修复的回归测试。

每一条都对应一个「代码看着对、实际跑起来错」的缺陷，所以断言必须落在
**可观察的行为**上（返回码 / 返回体 / 连接上的下一行请求 / 落地文件内容），
不能只看函数被调用过。
"""
import json
import os
import tempfile
import unittest

import support  # noqa: F401

import http_api
import verify
from test_bindings import cloud_tree, make_ctx, make_store, ready_ctx
from test_openlist import FakeResp, FakeTransport, PatchTransport, make_client


def split_statuses(raw):
    """把一条假 socket 上的多个响应切成状态码列表（按 Content-Length 走）。"""
    out = []
    while raw:
        head, sep, rest = raw.partition(b"\r\n\r\n")
        if not head:
            break
        line = head.split(b"\r\n")[0].split(b" ")
        status = int(line[1]) if len(line) > 1 and line[1].isdigit() else 0
        length = 0
        for h in head.split(b"\r\n")[1:]:
            if h.lower().startswith(b"content-length:"):
                length = int(h.split(b":", 1)[1])
        out.append(status)
        raw = rest[length:]
        if not sep:
            break
    return out


# -------------------------------------------------------------------------------
# 1. 非标准响应不再抛 NameError（旧代码引用了一个不存在的变量名）
# -------------------------------------------------------------------------------

class NonStandardResponseTest(unittest.TestCase):
    def test_html_error_page_raises_openlist_error(self):
        import openlist
        transport = FakeTransport()
        transport.push("POST", "/api/fs/list", b"<html>gateway error</html>")
        client, _ = make_client(transport)
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.list_dir("/x")
        # 关键：是「说得清原因的业务错误」，不是 500「服务器内部错误」
        self.assertIn("非 OpenList 响应", str(cm.exception))
        self.assertNotIsInstance(cm.exception, NameError)

    def test_gateway_3xx_status_also_raises(self):
        """3xx（没跟随后落到这里）同样属于「不是正常 OpenList 响应」。"""
        import openlist
        transport = FakeTransport()
        transport.push("POST", "/api/fs/list", b"<html>moved</html>", status=302)
        client, _ = make_client(transport)
        with PatchTransport(transport):
            with self.assertRaises(openlist.OpenlistError) as cm:
                client.list_dir("/x")
        self.assertIn("非 OpenList 响应", str(cm.exception))


# -------------------------------------------------------------------------------
# 2. /api/log 只读文件尾部（旧实现 readlines() 整份读入）
# -------------------------------------------------------------------------------

class TailLinesTest(unittest.TestCase):
    def test_reads_last_lines_of_big_file(self):
        path = os.path.join(tempfile.mkdtemp(prefix="dd-log-"), "info.log")
        with open(path, "wb") as fh:
            for i in range(20000):
                fh.write(("第 %d 行 payload\n" % i).encode("utf-8"))
        lines = http_api._tail_lines(path, 50)
        self.assertEqual(len(lines), 50)
        self.assertTrue(lines[-1].startswith("第 19999 行"))
        self.assertTrue(lines[0].startswith("第 19950 行"))

    def test_missing_file_and_empty_file(self):
        self.assertEqual(http_api._tail_lines("/nonexistent/x.log", 10), [])
        path = os.path.join(tempfile.mkdtemp(prefix="dd-log-"), "empty.log")
        open(path, "wb").close()
        self.assertEqual(http_api._tail_lines(path, 10), [])


# -------------------------------------------------------------------------------
# 3. keep-alive：未知 POST 路由必须把请求体读掉，否则下一行请求被污染
# -------------------------------------------------------------------------------

class KeepAliveDrainTest(unittest.TestCase):
    def _ctx(self):
        from test_api import make_ctx as make_api_ctx
        return make_api_ctx()

    def test_unknown_post_then_get_on_same_connection(self):
        ctx = self._ctx()
        payload = (support.build_request("POST", "/api/no-such-route",
                                         body={"id": "task-1", "x": [1, 2, 3]})
                   + support.build_request("GET", "/api/info"))
        sock = support.FakeSocket(payload)
        http_api.AppHandler(sock, ("127.0.0.1", 54321), support.FakeServer(ctx))
        self.assertEqual(split_statuses(sock.get_out()), [404, 200])

    def test_chunked_body_is_rejected_not_silently_empty(self):
        ctx = self._ctx()
        raw = (b"POST /api/log/clear HTTP/1.1\r\n"
               b"Host: localhost\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n"
               b"5\r\nhello\r\n0\r\n\r\n")
        sock = support.FakeSocket(raw)
        http_api.AppHandler(sock, ("127.0.0.1", 54321), support.FakeServer(ctx))
        self.assertEqual(split_statuses(sock.get_out())[0], 411)


# -------------------------------------------------------------------------------
# 4. 云端扫描：单个子目录失败不再让整场扫描作废
# -------------------------------------------------------------------------------

class ScanCloudTolerantTest(unittest.TestCase):
    def _client(self):
        tree = cloud_tree("/source", {"a.txt": 100, "sub/b.txt": 200})
        ctx = make_ctx(tree=tree, scan_errors={"/source/sub": "权限不足"})
        return ctx._client

    def test_strict_false_keeps_partial_result(self):
        client = self._client()
        errors = []
        out = verify.scan_cloud(client, "/source", 3, [], strict=False,
                                on_error=lambda p, rel, exc: errors.append((p, rel, str(exc))))
        self.assertIn("a.txt", out)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "sub")
        self.assertIn("权限不足", errors[0][2])

    def test_strict_true_still_raises(self):
        client = self._client()
        with self.assertRaises(RuntimeError):
            verify.scan_cloud(client, "/source", 3, [])


# -------------------------------------------------------------------------------
# 5. 绑定：只改名字不能把扫描结果清空
# -------------------------------------------------------------------------------

class BindingUpdateKeepsScanTest(unittest.TestCase):
    def test_rename_keeps_files_and_last_scanned_at(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100, "b.txt": 200})
        before = store.get_binding(bid)
        files_before = len(before["files"])
        scanned_before = before["lastScannedAt"]
        self.assertGreater(files_before, 0)

        # 前端保存时会把整份表单回传；这里模拟"老前端"：目标只给 path/enabled，不带 id
        store.update_binding(bid, {
            "name": "改个名字",
            "cloudTargets": [{"path": "/backup", "enabled": True}],
        })
        after = store.get_binding(bid)
        self.assertEqual(after["name"], "改个名字")
        self.assertEqual(len(after["files"]), files_before)
        self.assertEqual(after["lastScannedAt"], scanned_before)

    def test_target_id_survives_update(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        tid_before = store.get_binding(bid)["cloudTargets"][0]["id"]
        store.update_binding(bid, {"cloudTargets": [{"path": "/backup", "enabled": True}]})
        self.assertEqual(store.get_binding(bid)["cloudTargets"][0]["id"], tid_before)


# -------------------------------------------------------------------------------
# 6. 部分扫描失败要如实标出来（不能当成"全部同步好了"）
# -------------------------------------------------------------------------------

class ScanWarningTest(unittest.TestCase):
    def test_partial_failure_sets_warning(self):
        tree = cloud_tree("/source", {"a.txt": 100, "sub/b.txt": 200})
        tree.update(cloud_tree("/backup", {}))
        ctx = make_ctx(tree=tree, scan_errors={"/source/sub": "权限不足"})
        store = make_store(ctx)
        b = store.create_binding({"localPath": "/source", "cloudTargets": ["/backup"]})
        store.start_scan(b["id"], background=False)
        detail = store.get_binding(b["id"])
        self.assertEqual(detail["scanStatus"], "done")
        self.assertIn("权限不足", detail["scanWarning"])
        # 能读到的部分照常参与比对
        self.assertGreater(detail["totalLocalFiles"], 0)

    def test_clean_scan_has_no_warning(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        self.assertEqual(store.get_binding(bid)["scanWarning"], "")


if __name__ == "__main__":
    unittest.main()
