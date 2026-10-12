# -*- coding: utf-8 -*-
"""httpclient 传输层测试：重点盯 curl 后端的失败判定。

`_probe_curl` 真实 fork 进程去问 `curl --version`，在 CI / 开发机上不一定有，
所以这里**不测探测本身**，只测拿到 curl 结果之后的判定逻辑 —— 那才是会静默
出错的地方：`returncode != 0` 但 stdout 里已经吐了半截响应的情况。
"""
import unittest

import support  # noqa: F401

import httpclient


class FakeProc(object):
    """替掉 subprocess.Popen 的返回值。"""

    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False
        self.communicate_calls = []

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append((input, timeout))
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


class FakeSubprocess(object):
    """只需要 Popen 与 TimeoutExpired —— `_curl_request` 的两个 except 会引用后者。"""

    TimeoutExpired = httpclient.subprocess.TimeoutExpired
    PIPE = httpclient.subprocess.PIPE

    def __init__(self, proc):
        self.proc = proc
        self.popen_args = None

    def Popen(self, args, **kwargs):
        self.popen_args = args
        return self.proc


def _patch(proc):
    real = httpclient.subprocess
    fake = FakeSubprocess(proc)
    httpclient.subprocess = fake
    return real, fake


def _unpatch(real):
    httpclient.subprocess = real


class CurlReturncodeTest(unittest.TestCase):
    """curl 退出码非 0 一律报错 —— **不管它有没有吐出东西**。

    旧逻辑只在「完全没有输出」时才报错。curl 中途失败（连接被重置、写到一半
    断掉、DNS 失败后重试）时常常已经吐了半截响应：一个缺了尾部的 JSON，或者
    只写完了响应头。那半截东西被当成真响应用下去的结果是 —— 界面不报错，
    但目录列表莫名少了一半，用户会以为「云端真的没有这些文件」。
    """

    def call(self, proc):
        real, _ = _patch(proc)
        try:
            return httpclient._curl_request(
                "POST", "http://192.168.0.10:5244/api/fs/list",
                {"Content-Type": "application/json"}, b'{"path":"/"}', 30)
        finally:
            _unpatch(real)

    def _half_json_body(self):
        return (b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"\r\n"
                b'{"code":200,"data":{"content":[{"name":"a"')

    def test_nonzero_returncode_with_output_still_raises(self):
        """有输出但退出码非 0 → 照报错（这是本次修复的核心）。"""
        proc = FakeProc(28, stdout=self._half_json_body(),
                        stderr=b"curl: (28) Operation timed out")
        with self.assertRaises(httpclient.TransportError) as cm:
            self.call(proc)
        self.assertIn("请求失败", str(cm.exception))
        # 错误信息里要带上 curl 自己说的原因，用户拿去搜才有意义
        self.assertIn("timed out", str(cm.exception))

    def test_nonzero_returncode_without_stderr_uses_exit_code(self):
        """curl 什么都没解释时，至少把退出码说出来，别报一个空错误。"""
        proc = FakeProc(7, stdout=b"", stderr=b"")
        with self.assertRaises(httpclient.TransportError) as cm:
            self.call(proc)
        self.assertIn("7", str(cm.exception))

    def test_zero_returncode_parses_response(self):
        """退出码 0 → 正常解析响应（反向用例：判据不能「有输出就算成功」，
        也不能误伤正常路径）。"""
        body = (b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"\r\n"
                b'{"code":200,"data":{"content":[]}}')
        proc = FakeProc(0, stdout=body, stderr=b"")
        resp = self.call(proc)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read_all(), b'{"code":200,"data":{"content":[]}}')

    def test_empty_output_ok_returns_empty_response(self):
        """curl 成功但没有任何输出（HEAD / 204）：返回空响应，不能算失败。"""
        proc = FakeProc(0, stdout=b"", stderr=b"")
        resp = self.call(proc)
        self.assertEqual(resp.status, 0)
        self.assertEqual(resp.read_all(), b"")

    def test_body_without_header_split_is_not_mistaken_for_headers(self):
        """只有响应体、没有 \\r\\n\\r\\n 分隔时不该把正文当头解析。"""
        proc = FakeProc(0, stdout=b"garbage-without-headers", stderr=b"")
        resp = self.call(proc)
        self.assertEqual(resp.status, 0)
        self.assertEqual(resp.read_all(), b"garbage-without-headers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
