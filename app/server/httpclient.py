# -*- coding: utf-8 -*-
"""HTTP 传输层：优先 urllib，ssl 不可用时自动退到系统 curl。

同样是飞牛裁剪构建的坑：`urllib.request` 走 HTTPS 需要 `ssl` 扩展，而裁剪版
python312 不一定编入了它。这里在启动时做**功能性探测**（不是只看能不能 import），
探不到就换成 `curl`（Debian 系 NAS 上基本都有）。

对外统一返回 `HttpResponse`，屏蔽两种后端的行为差异。
"""

import os
import subprocess
import threading
import time

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_TIMEOUT = 60


# --------------------------------------------------------------------------
# 能力探测
# --------------------------------------------------------------------------

def _probe_urllib_ssl():
    """真的建一个 HTTPS 上下文才算可用。"""
    try:
        import ssl                          # noqa: F401  软导入，失败不能拖垮进程
        import urllib.request as _ur         # noqa: F401
        ctx = ssl.create_default_context()
        if ctx is None:
            return False
        # 有些裁剪构建里 ssl 能 import，但缺少 _ssl 扩展，实例化时才炸
        return hasattr(ctx, "wrap_socket")
    except Exception:
        return False


def _probe_curl():
    for name in ("curl", "/usr/bin/curl", "/bin/curl"):
        try:
            out = subprocess.run([name, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=10)
            if out.returncode == 0:
                return name
        except Exception:
            continue
    return None


_BACKEND = None
_CURL = None
_BACKEND_NOTE = ""
_PLAIN_HTTP_OK = None       # http:// 能否直接用 urllib（不需要 ssl）
_PROBE_LOCK = threading.Lock()


def _probe_plain_urllib():
    """http:// 用 urllib 不依赖 ssl，只要 http.client / urllib.request 在就行。"""
    global _PLAIN_HTTP_OK
    if _PLAIN_HTTP_OK is not None:
        return _PLAIN_HTTP_OK
    with _PROBE_LOCK:
        if _PLAIN_HTTP_OK is None:
            try:
                import http.client                     # noqa: F401
                import urllib.request                   # noqa: F401
                _PLAIN_HTTP_OK = True
            except Exception:
                _PLAIN_HTTP_OK = False
    return _PLAIN_HTTP_OK


def _url_scheme(url):
    """只取 scheme，不走 urlsplit（热路径，且畸形 URL 不该在这里抛异常）。"""
    idx = url.find("://")
    return url[:idx].lower() if idx > 0 else ""


def detect_backend():
    """返回 ('urllib'|'curl', 说明)。结果缓存。"""
    global _BACKEND, _CURL, _BACKEND_NOTE
    if _BACKEND is not None:
        return _BACKEND, _BACKEND_NOTE

    if _probe_urllib_ssl():
        _BACKEND = "urllib"
        _BACKEND_NOTE = "urllib + ssl"
        return _BACKEND, _BACKEND_NOTE

    _CURL = _probe_curl()
    if _CURL:
        _BACKEND = "curl"
        _BACKEND_NOTE = "urllib 的 ssl 不可用，HTTPS 请求降级到 %s" % _CURL
        return _BACKEND, _BACKEND_NOTE

    _BACKEND = "urllib"
    _BACKEND_NOTE = "ssl 与 curl 都不可用，HTTPS 请求会失败"
    return _BACKEND, _BACKEND_NOTE


def pick_backend(url):
    """按 URL 的 scheme 选传输后端 —— **不要**直接看 detect_backend()。

    `detect_backend()` 回答的是「能不能走 HTTPS」，而飞牛的裁剪版 python312
    常常缺 `_ssl`。老写法在那种机器上把**内网 http:// 请求也判成要走 curl**，
    于是每发一次请求就 fork 一个 curl 子进程。云端扫描是「每目录一次请求」，
    一个几百目录的云端树 = 几百次进程创建 + 几百次 TLS 无关的握手，这是内网
    场景下最亏的一笔开销。

    正确判断：http:// 压根不需要 ssl，一律走 urllib；只有 https:// 且 ssl
    不可用时才真的需要 curl。
    """
    if _url_scheme(url) == "http" and _probe_plain_urllib():
        return "urllib"
    backend, _ = detect_backend()
    return backend


class TransportError(Exception):
    """网络层错误（连不上、超时、证书问题……），与业务错误区分开。"""

    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


# --------------------------------------------------------------------------
# 统一响应对象
# --------------------------------------------------------------------------

class HttpResponse(object):
    def __init__(self, status, headers, reader, closer):
        self.status = status
        self.headers = headers or {}
        self._reader = reader
        self._closer = closer

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    def read(self, size=-1):
        return self._reader(size)

    def iter_chunks(self, chunk_size=256 * 1024):
        while True:
            chunk = self._reader(chunk_size)
            if not chunk:
                break
            yield chunk

    def read_all(self):
        parts = []
        for chunk in self.iter_chunks():
            parts.append(chunk)
        return b"".join(parts)

    def close(self):
        if self._closer:
            try:
                self._closer()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _normalize_headers(raw_items):
    out = {}
    for k, v in raw_items:
        out[k.lower()] = v
    return out


def _parse_curl_headers(blob):
    """curl -D 输出解析；处理 100-continue 与重定向产生的多段头。"""
    blocks = [b for b in blob.split(b"\r\n\r\n") if b.strip()]
    if not blocks:
        return 0, {}
    last = blocks[-1]
    lines = last.decode("iso-8859-1").split("\r\n")
    status = 0
    headers = {}
    for line in lines:
        if not line.strip():
            continue
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
            headers = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


# --------------------------------------------------------------------------
# 请求入口
# --------------------------------------------------------------------------

def request(method, url, headers=None, body=None, timeout=_DEFAULT_TIMEOUT):
    """发起请求并流式返回。

    `body` 可以是 None / bytes / 可迭代的 bytes 分片（会走 chunked 或一次性拼接）。
    """
    backend = pick_backend(url)
    method = method.upper()
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)

    if backend == "curl":
        return _curl_request(method, url, headers, body, timeout)
    return _urllib_request(method, url, headers, body, timeout)


def _body_to_bytes(body):
    if body is None:
        return None
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    if hasattr(body, "read"):
        return body.read()
    # 可迭代分片
    parts = []
    for piece in body:
        parts.append(piece if isinstance(piece, bytes) else str(piece).encode("utf-8"))
    return b"".join(parts)


_OPENER = None
_OPENER_LOCK = threading.Lock()


def _plain_opener():
    """复用同一个 opener：build_opener 每次都要装配一遍 handler 链，
    扫描时被放大到上千次，没必要。显式清空代理是为了绕开开发机上
    劫持 127.0.0.1 的 HTTP 代理。"""
    global _OPENER
    if _OPENER is None:
        with _OPENER_LOCK:
            if _OPENER is None:
                import urllib.request
                _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def _urllib_request(method, url, headers, body, timeout):
    import urllib.request
    import urllib.error

    data = _body_to_bytes(body)
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        resp = _plain_opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是合法响应，交给上层按业务码处理
        return HttpResponse(exc.code, _normalize_headers(exc.headers.items()),
                            exc.read, exc.close)
    except Exception as exc:
        raise TransportError("请求失败：%s（%s）" % (exc, url), None)

    return HttpResponse(
        resp.status,
        _normalize_headers(resp.headers.items()),
        resp.read,
        resp.close,
    )


def _curl_request(method, url, headers, body, timeout):
    args = [_CURL or "curl", "-sS", "--noproxy", "*", "--max-time", str(int(timeout)),
            "-D", "-", "-X", method]
    for k, v in headers.items():
        args += ["-H", "%s: %s" % (k, v)]
    payload = _body_to_bytes(body)
    if payload:
        args += ["--data-binary", "@-"]

    try:
        # 用 communicate 替代手动写 stdin + 读 stdout/stderr：
        # communicate 会并行消费 stdout 和 stderr，避免「stderr 写满 → curl 阻塞
        # → 主进程阻塞在 stdout.read()」的双向死锁。
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except Exception as exc:
        raise TransportError("无法启动 curl：%s" % exc, None)

    try:
        blob, err = proc.communicate(input=payload, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TransportError("curl 请求超时（%ds）" % timeout, None)
    except Exception as exc:
        raise TransportError("curl 通信失败：%s" % exc, None)

    # returncode != 0 一律报错，不管有没有输出。
    # 旧逻辑只在「完全没输出」时才报错，curl 中途失败的半截 JSON 会被当真响应。
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or ("curl 退出码 %d" % proc.returncode)
        raise TransportError("请求失败：%s" % msg, None)

    if not blob:
        return HttpResponse(0, {}, _BytesReader(b"").read, None)

    # 找到最后一段响应头（跳过 100-continue 与重定向）。
    # _parse_curl_headers 内部已处理多段头，这里只需找到最后一个 \r\n\r\n 的位置。
    split_at = blob.rfind(b"\r\n\r\n")
    if split_at < 0:
        return HttpResponse(0, {}, _BytesReader(blob).read, None)

    status, hdrs = _parse_curl_headers(blob[:split_at + 4])
    reader = _BytesReader(blob[split_at + 4:])
    return HttpResponse(status, hdrs, reader.read, reader.close)


class _BytesReader(object):
    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


def retry(callable_, attempts=3, base_delay=0.6, on_error=None):
    """轻量重试。只对 TransportError 与 5xx 重试，业务错误不重试。"""
    last = None
    for i in range(attempts):
        try:
            return callable_()
        except TransportError as exc:
            last = exc
        except Exception as exc:
            if on_error:
                return on_error(exc)
            raise
        if i < attempts - 1:
            time.sleep(base_delay * (2 ** i))
    raise last


# --------------------------------------------------------------------------
# 流式上传（大文件不能走 request() —— 它会把整个 body 读进内存）
# --------------------------------------------------------------------------

class _ReaderAdapter(object):
    """把 bytes / 文件对象统一成「read(n) -> bytes」。"""

    def __init__(self, body):
        self._body = body
        self._pos = 0

    def read(self, size):
        body = self._body
        if hasattr(body, "read"):
            return body.read(size)
        if isinstance(body, (bytes, bytearray)):
            chunk = bytes(body[self._pos:self._pos + size])
            self._pos += len(chunk)
            return chunk
        raise TransportError("不支持的上传数据源类型：%s" % type(body).__name__, None)


def put_stream(url, headers, body, size, on_progress=None, timeout=_DEFAULT_TIMEOUT,
               chunk_size=1024 * 1024, method="PUT"):
    """流式上传，分块发送并在每块后回调进度。

    为什么单独写一条路径而不是复用 request()：
    request() 的 `_body_to_bytes` 会把整个文件读进内存，几个 GB 的文件直接把
    内存撑爆。这里用 http.client 手动 putrequest/send 循环，内存占用恒定在
    chunk_size 量级。OpenList 在内网是 HTTP，http.client 不需要 ssl，天然绕开
    裁剪构建缺 `_ssl` 的坑（HTTPS 时才探测 ssl）。

    body: bytes 或文件对象（有 .read）。
    size: 总字节数，写入 Content-Length。
    on_progress(sent, total): 每块发送成功后回调。
    返回 HttpResponse。
    """
    import http.client
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname
    if not host:
        raise TransportError("上传地址不合法：%s" % url, None)
    port = parts.port or (443 if scheme == "https" else 80)
    # 只保留 path + query，netloc 由连接对象负责
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))

    if scheme == "https":
        if not _probe_urllib_ssl():
            raise TransportError("当前 python 的 ssl 不可用，无法向 HTTPS 地址上传", None)
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    reader = _ReaderAdapter(body)
    try:
        conn.putrequest(method, target)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(int(size)))
        conn.putheader("User-Agent", USER_AGENT)
        conn.endheaders()

        sent = 0
        while True:
            buf = reader.read(chunk_size)
            if not buf:
                break
            conn.send(buf)
            sent += len(buf)
            if on_progress:
                on_progress(sent, int(size))

        resp = conn.getresponse()
        return HttpResponse(resp.status, _normalize_headers(resp.getheaders()),
                            resp.read, conn.close)
    except TransportError:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        raise TransportError("上传失败：%s（%s）" % (exc, url), None)
