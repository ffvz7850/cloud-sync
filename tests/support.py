# -*- coding: utf-8 -*-
"""测试公共设施。

核心是 `call_handler`：**进程内直接驱动 BaseHTTPRequestHandler**，不占端口、不依赖网络，
比起真服务快得多也稳得多（开发机上的 HTTP 代理还经常劫持 127.0.0.1，起真服务会莫名失败）。

两个必须踩对的点（都在 fpk 踩坑清单 D 节里记过）：
1. 假 socket 必须实现 `makefile('rb')`、`sendall`、`write`、`flush`、`settimeout`、`setsockopt`
   —— HTTP/1.1 且 wbufsize=0 时走 `_SocketWriter`，它调的是 `sendall`。
2. **构造函数内部就会跑完整个 `handle()`**（一直读到 EOF），
   所以不要再手动调 `handle_one_request()`，否则会报 I/O operation on closed file。
"""

import io
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SERVER_DIR = os.path.join(ROOT, "app", "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


class FakeSocket(object):
    def __init__(self, payload):
        self._in = io.BytesIO(payload)
        self._out = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self._in
        return self._out

    def sendall(self, data):
        self._out.write(data)

    def write(self, data):
        self._out.write(data)

    def flush(self):
        pass

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def shutdown(self, *args):
        pass

    def close(self):
        pass

    def fileno(self):
        return 0

    def getsockname(self):
        return ("127.0.0.1", 12345)

    def get_out(self):
        return self._out.getvalue()


class FakeServer(object):
    """Handler 通过 self.server.ctx 取上下文，所以只需要一个带 ctx 的壳。"""

    def __init__(self, ctx):
        self.ctx = ctx


def build_request(method, path, body=None, headers=None):
    headers = dict(headers or {})
    payload = b""
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
    headers.setdefault("Host", "localhost")
    headers["Content-Length"] = str(len(payload))

    lines = ["%s %s HTTP/1.1" % (method, path)]
    for key, value in headers.items():
        lines.append("%s: %s" % (key, value))
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8") + payload


def parse_response(raw):
    if not raw:
        return 0, {}, b""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = 0
    if lines and lines[0].upper().startswith("HTTP/"):
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


def call_handler(handler_cls, ctx, method, path, body=None, headers=None):
    """返回 (status, headers, body_bytes)。"""
    payload = build_request(method, path, body=body, headers=headers)
    sock = FakeSocket(payload)

    # BaseHTTPRequestHandler 的构造函数里就把请求处理完了
    handler_cls(sock, ("127.0.0.1", 54321), FakeServer(ctx))
    return parse_response(sock.get_out())


def json_body(body):
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def temp_data_dir(prefix="dd-test-"):
    """建一个隔离的数据目录，并让 config 模块指向它。

    注意：`config` 里的路径函数每次都会重新读环境变量，所以改环境变量就够，
    不需要 reload 模块。
    """
    import tempfile
    path = tempfile.mkdtemp(prefix=prefix)
    os.environ["TRIM_PKGVAR"] = path
    os.environ["TRIM_PKGETC"] = os.path.join(path, "etc")
    os.environ["TRIM_PKGTMP"] = os.path.join(path, "tmp")
    os.makedirs(os.path.join(path, "etc"), exist_ok=True)
    os.makedirs(os.path.join(path, "tmp"), exist_ok=True)
    return path


def cleanup_env():
    for key in ("TRIM_PKGVAR", "TRIM_PKGETC", "TRIM_PKGTMP",
                "TRIM_APPDEST", "TRIM_DATA_ACCESSIBLE_PATHS"):
        os.environ.pop(key, None)
