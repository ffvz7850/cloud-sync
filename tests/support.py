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


# ---------------------------------------------------------------------------
# 假 OpenList 任务服务端（按真实语义实现）
# ---------------------------------------------------------------------------

class FakeTaskResp(object):
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read_all(self):
        return self._raw

    def read(self, n=-1):
        return self._raw

    def close(self):
        pass


class FakeTaskServer(object):
    """**按真实 OpenList 语义**实现 `/api/task/<type>/*` 的假服务端。

    用来替换 `httpclient.request`（`client = OpenlistClient(store)`，
    `with PatchTransport(server)` 里调）。

    三条语义必须一丝不差 —— 差一条，「全部取消不生效」这类缺陷就测不出来：

    1. 批量接口（`cancel_some` / `delete_some`）**永远返回 HTTP 200 + code 200**，
       每个 tid 的真实结果藏在 `data` 里：成功是 `null`，失败是一句原因。
       所以「HTTP 通了」≠「任务停了」。
    2. 只有**真的存在**的 tid 才真的被取消/删除。`stuck` 里的 tid 是"永远取消
       不掉"的那种（模拟服务端已经失去对该任务的控制）。
    3. 单条接口对不存在的 tid 返回 404，这样"逐条兜底"才能得出确定结论。

    另外：`data` 返回 None（`no_batch_data=True`）用来模拟"服务端根本不理批量
    请求体"的部署 —— 这时客户端必须承认「不知道」，不能算成功。
    """

    def __init__(self, undone=None, done=None, stuck=None, no_batch_data=False):
        self.undone = {"copy": [], "upload": []}
        self.done = {"copy": [], "upload": []}
        self.stuck = set(str(t) for t in (stuck or ()))
        self.no_batch_data = no_batch_data
        self.calls = []          # [(method, url)]，断言调用序列用
        self.batch_bodies = []   # 批量接口收到的 body（形状断言用）
        for task in undone or ():
            self.add(task)

    # -- 数据准备 --

    def add(self, task, task_type="copy"):
        task = dict(task)
        task.setdefault("state", 1)
        task.setdefault("name", task.get("id") or "task")
        self.undone.setdefault(task_type, []).append(task)
        return task

    def add_done(self, task, task_type="copy"):
        task = dict(task)
        task.setdefault("state", 2)
        self.done.setdefault(task_type, []).append(task)
        return task

    def ids(self, task_type="copy"):
        return [t["id"] for t in self.undone.get(task_type, [])]

    # -- 内部 --

    @staticmethod
    def _path(url):
        """取出 URL 里的路径部分（**去掉查询串**，否则 action 会带上 "?tid="）。"""
        parts = url.split("://", 1)
        rest = parts[1] if len(parts) > 1 else url
        path = rest.split("/", 1)
        out = "/" + (path[1] if len(path) > 1 else "")
        return out.split("?", 1)[0]

    @staticmethod
    def _query(url):
        if "?" not in url:
            return {}
        from urllib.parse import parse_qs
        return parse_qs(url.split("?", 1)[1], keep_blank_values=True)

    def _single(self, action, task_type, tid):
        """单条取消 / 删除 / 重试。存在才生效，不存在 404。"""
        self.calls.append(("POST", "%s?tid=%s" % (action, tid)))
        bucket = self.undone.get(task_type, [])
        hit = [t for t in bucket if str(t.get("id")) == tid]
        if not hit:
            return FakeTaskResp(404, {"code": 404, "message": "task not found",
                                      "data": None})
        if tid in self.stuck:
            return FakeTaskResp(400, {"code": 400, "message": "task is stuck",
                                      "data": None})
        if action == "cancel":
            bucket.remove(hit[0])
            self.done.setdefault(task_type, []).append(dict(hit[0], state=4))
        elif action == "delete":
            bucket.remove(hit[0])
        return FakeTaskResp(200, {"code": 200, "message": "success", "data": None})

    def _batch(self, action, task_type, tids):
        self.batch_bodies.append({"action": action, "type": task_type,
                                  "body": list(tids)})
        self.calls.append(("POST", "%s_some[%d]" % (action, len(tids))))
        errors = {}
        for tid in tids:
            tid = str(tid)
            bucket = self.undone.get(task_type, [])
            hit = [t for t in bucket if str(t.get("id")) == tid]
            if not hit:
                errors[tid] = "task not found"
                continue
            if tid in self.stuck:
                errors[tid] = "task is stuck"
                continue
            if action == "cancel":
                bucket.remove(hit[0])
                self.done.setdefault(task_type, []).append(dict(hit[0], state=4))
            elif action == "delete":
                bucket.remove(hit[0])
            errors[tid] = None
        if self.no_batch_data:
            return FakeTaskResp(200, {"code": 200, "message": "success", "data": None})
        # Go 的 map[string]error：成功是 null，失败是字符串
        return FakeTaskResp(200, {"code": 200, "message": "success", "data": errors})

    # -- 传输层入口 --

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        import json as _json
        path = self._path(url)
        query = self._query(url)

        if path.startswith("/api/task/"):
            rest = path[len("/api/task/"):].split("/")
            task_type = rest[0]
            action = rest[1] if len(rest) > 1 else ""
            if method == "GET" and action in ("undone", "done"):
                return FakeTaskResp(200, {"code": 200, "message": "success",
                                          "data": list(self.undone.get(task_type, []))
                                          if action == "undone"
                                          else list(self.done.get(task_type, []))})
            if method == "POST":
                if action in ("cancel_some", "delete_some"):
                    payload = {}
                    try:
                        payload = _json.loads((body or b"{}").decode("utf-8"))
                    except Exception:
                        payload = {}
                    tids = payload.get("tids") if isinstance(payload, dict) else payload
                    if not isinstance(tids, list):
                        tids = []
                    return self._batch(action[:-len("_some")], task_type, tids)
                if action in ("cancel", "delete", "retry"):
                    tid = (query.get("tid") or [""])[0]
                    return self._single(action, task_type, tid)
                if action == "retry_failed":
                    # 把 done 里失败的任务搬回 undone（真实语义）
                    self.calls.append(("POST", "retry_failed:%s" % task_type))
                    keep = []
                    for task in self.done.get(task_type, []):
                        if int(task.get("state") or 0) in (5, 6, 7):
                            self.undone.setdefault(task_type, []).append(
                                dict(task, state=0))
                        else:
                            keep.append(task)
                    self.done[task_type] = keep
                    return FakeTaskResp(200, {"code": 200, "message": "success",
                                              "data": None})
                if action == "clear_done":
                    self.done[task_type] = []
                    return FakeTaskResp(200, {"code": 200, "message": "success",
                                              "data": None})

        return FakeTaskResp(404, {"code": 404,
                                 "message": "not found: %s" % url, "data": None})


class PatchTransport(object):
    """with 块内把 `httpclient.request` 换成假服务端，退出时还原。"""

    def __init__(self, transport):
        self.transport = transport
        self.orig = None

    def __enter__(self):
        import httpclient
        self.orig = httpclient.request
        httpclient.request = self.transport
        return self.transport

    def __exit__(self, *args):
        import httpclient
        httpclient.request = self.orig
        return False


def make_openlist_client(base_url="http://nas:5244", token="TOK-1"):
    """造一个已配置好凭据的真 `OpenlistClient`（配合 PatchTransport 使用）。

    已经设过 `TRIM_PKGVAR` 就沿用它 —— 调用方（比如 http_api 的用例）往往先
    建好了沙盒目录，顺手把它换掉会让同一次用例里的两处 state 指向不同目录。
    """
    import openlist
    var = os.environ.get("TRIM_PKGVAR")
    if not var or not os.path.isdir(var):
        var = temp_data_dir()
    store = openlist.CredentialStore(os.path.join(var, "cred.json"))
    store.save({"baseUrl": base_url, "username": "alice",
                "passwordHash": openlist.static_hash("secret"), "token": token})
    return openlist.OpenlistClient(store)

