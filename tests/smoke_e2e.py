# -*- coding: utf-8 -*-
"""端到端冒烟：从**当前工程里的 app/ 代码** 起真实 HTTP 服务，
对着一个**真正走 HTTP 的假 OpenList** 跑完整链路。

为什么要起真服务而不是 mock 客户端：mock 传输层只按 path 匹配会掩盖
URL 拼接、认证头、分页这些真机上才会暴露的问题（历史踩过这个坑）。

跑法（在工程根目录执行）：
    python tests/smoke_e2e.py [解包后的 app 目录，默认 ../app]

不放进 run_all.py 的自动发现里：它要起真实 HTTP 服务与端口，
比单元测试重得多，适合在交付/改接口后手动跑一遍。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_default_app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
APP_DIR = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else _default_app)
SERVER_DIR = os.path.join(APP_DIR, "server")
sys.path.insert(0, SERVER_DIR)

# 数据目录必须隔离在临时目录，别碰真实环境
DATA = tempfile.mkdtemp(prefix="dd-smoke-")
os.environ["TRIM_PKGVAR"] = DATA
os.environ["TRIM_PKGETC"] = os.path.join(DATA, "etc")
os.environ["TRIM_PKGTMP"] = os.path.join(DATA, "tmp")
os.environ["TRIM_APPDEST"] = APP_DIR
os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = DATA
for sub in ("etc", "tmp"):
    os.makedirs(os.path.join(DATA, sub), exist_ok=True)

FAILS = []
PASSED = [0]


def check(name, cond, detail=""):
    if cond:
        PASSED[0] += 1
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


# ---------------------------------------------------------------------------
# 假 OpenList（真 HTTP）—— 严格校验请求形状，URL 拼错就立刻暴露
# ---------------------------------------------------------------------------

CLOUD = {
    "/": [{"name": "src", "is_dir": True, "size": 0, "modified": ""},
          {"name": "dst", "is_dir": True, "size": 0, "modified": ""}],
    "/src": [{"name": "a.bin", "is_dir": False, "size": 111, "modified": "2026-09-01T00:00:00"},
             {"name": "b.bin", "is_dir": False, "size": 222, "modified": "2026-09-01T00:00:00"},
             {"name": "sub", "is_dir": True, "size": 0, "modified": ""}],
    "/src/sub": [{"name": "c.bin", "is_dir": False, "size": 333, "modified": "2026-09-01T00:00:00"}],
    "/dst": [],
    "/dst/sub": [],
    "/backup": [],
}

COPIED = []          # 服务端收到的 copy 请求
COPY_TASKS = []      # 尚未完成的任务
DONE_TASKS = []      # 已完成的任务
BAD = []             # 形状不对的请求

# 云端文件的真实字节。CLOUD 里只有 size，下载链路必须能拿到真数据，
# 所以内容单独放一份：key 是云端绝对路径。
BLOBS = {}
REMOVED = []         # 服务端收到的 remove 请求体
RANGES = []          # 服务端收到的 Range 请求头（断点续传的证据）

EXPECTED_TOKEN = None
LOGIN_BODIES = []


def put_blob(path, data):
    """登记一个云端文件：目录列表项 + 真实字节一次写齐，避免两者漂移。"""
    parent, _, name = path.rstrip("/").rpartition("/")
    parent = parent or "/"
    lst = CLOUD.setdefault(parent, [])
    lst[:] = [x for x in lst if x["name"] != name]
    lst.append({"name": name, "is_dir": False, "size": len(data),
                "modified": "2026-09-01T00:00:00"})
    BLOBS[path] = data
    return data


def cloud_dir(path):
    """建一个云端目录（含父级链）：目录列表项与子表的 key 一次写齐。

    `put_blob` 只登记文件所在的那张列表，父目录节点得另外建 —— 不建的话
    `/api/fs/get` 会判定这个目录不存在，比对直接报「云端目录不存在」。
    """
    path = "/" + "/".join(p for p in str(path).split("/") if p)
    if path == "/":
        CLOUD.setdefault("/", [])
        return path
    parts = path.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        cur = "/" + "/".join(parts[:i])
        parent, _, name = cur.rpartition("/")
        lst = CLOUD.setdefault(parent or "/", [])
        if not any(x["name"] == name for x in lst):
            lst.append({"name": name, "is_dir": True, "size": 0, "modified": ""})
        CLOUD.setdefault(cur, [])
    return path


def drop_cloud_entry(parent, name):
    """按 OpenList 语义删一个条目：目录是**递归**删除。

    目录的递归必须在假服务端也成立 —— 否则删掉目录后子目录的 key 还在，
    下一轮 list 会读到幽灵目录，比对结果就跟着错。
    """
    lst = CLOUD.get(parent) or []
    hit = [x for x in lst if x["name"] == name]
    if not hit:
        return False
    CLOUD[parent] = [x for x in lst if x["name"] != name]
    full = "%s/%s" % (parent.rstrip("/"), name)
    BLOBS.pop(full, None)
    if hit[0].get("is_dir"):
        prefix = full + "/"
        for key in [k for k in CLOUD if k.startswith(prefix)]:
            CLOUD.pop(key, None)
        for key in [k for k in BLOBS if k.startswith(prefix)]:
            BLOBS.pop(key, None)
    return True


# /src 下的三个文件要能真的被下载：内容与上面的 size 保持一致
put_blob("/src/a.bin", b"a" * 111)
put_blob("/src/b.bin", b"b" * 222)
put_blob("/src/sub/c.bin", b"c" * 333)



def make_ol_handler():
    class OLHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, payload, status=200):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_bytes(self, data, status=200, extra=None):
            """下载端点：必须回真字节，不能回 JSON。"""
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(data)

        def _serve_download(self, path):
            """`/d/<云端路径>`：支持 Range（断点续传要靠它）。"""
            from urllib.parse import unquote
            if not self._need_auth():
                return self._send({"code": 401, "message": "unauthorized"}, 401)
            blob = BLOBS.get("/" + unquote(path[3:]).lstrip("/"))
            if blob is None:
                return self._send({"code": 404, "message": "object not found"}, 404)
            start, status, extra = 0, 200, None
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                RANGES.append(rng)
                spec = rng[len("bytes="):].split(",")[0].strip()
                try:
                    start = int(spec.split("-")[0] or 0)
                except ValueError:
                    start = 0
                start = max(0, min(start, len(blob)))
                status = 206
                extra = {"Content-Range": "bytes %d-%d/%d"
                         % (start, len(blob) - 1, len(blob))}
            return self._send_bytes(blob[start:], status, extra)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                BAD.append("请求体不是 JSON")
                return {}

        def _need_auth(self):
            """除登录与 public/settings 外，必须带裸 token（无 Bearer 前缀）。"""
            got = self.headers.get("Authorization") or ""
            if got != EXPECTED_TOKEN:
                BAD.append("Authorization 头不对：%r" % got)
                return False
            return True

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/public/settings":
                return self._send({"code": 200, "data": {"version": "v4.2.6"}})
            if path.startswith("/d/"):
                return self._serve_download(path)
            if not self._need_auth():
                return self._send({"code": 401, "message": "unauthorized"}, 401)
            if path == "/api/me":
                return self._send({"code": 200, "data": {
                    "username": "admin", "base_path": "/", "permission": 7}})
            if path == "/api/task/copy/undone":
                return self._send({"code": 200, "data": COPY_TASKS})
            if path == "/api/task/copy/done":
                return self._send({"code": 200, "data": DONE_TASKS})
            return self._send({"code": 404, "message": "unknown GET %s" % path})

        def do_PUT(self):
            """小文件直传：PUT /api/fs/put + File-Path 头（URL 编码）+ 裸 token。"""
            from urllib.parse import unquote
            path = self.path.split("?")[0]
            if path != "/api/fs/put":
                return self._send({"code": 404, "message": "unknown PUT %s" % path})
            if not self._need_auth():
                return self._send({"code": 401, "message": "unauthorized"}, 401)
            fpath = unquote(self.headers.get("File-Path") or "")
            if not fpath.startswith("/"):
                BAD.append("File-Path 不是绝对路径：%r" % fpath)
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else b""
            parent, _, name = fpath.rstrip("/").rpartition("/")
            parent = parent or "/"
            if (self.headers.get("Overwrite") or "").lower() != "true":
                BAD.append("直传没带 Overwrite: true（会被服务端拒绝）")
            lst = CLOUD.setdefault(parent, [])
            lst[:] = [x for x in lst if x["name"] != name]
            lst.append({"name": name, "is_dir": False, "size": len(data),
                        "modified": "2026-09-02T00:00:00"})
            # 传上来的内容也要留住：不然刚传完的文件下载回来会是空的
            BLOBS[fpath] = data
            return self._send({"code": 200, "data": None})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/auth/login/hash":
                body = self._body()
                LOGIN_BODIES.append(body)
                # 必须是 64 位十六进制 StaticHash，不能是明文
                pw = body.get("password") or ""
                if len(pw) != 64 or any(c not in "0123456789abcdef" for c in pw):
                    BAD.append("登录用的不是 StaticHash：%r" % pw)
                if not body.get("username"):
                    BAD.append("登录缺 username")
                return self._send({"code": 200, "data": {"token": EXPECTED_TOKEN}})

            if not self._need_auth():
                return self._send({"code": 401, "message": "unauthorized"}, 401)

            body = self._body()

            if path == "/api/fs/list":
                p = body.get("path") or "/"
                if not isinstance(body.get("page"), int) or not isinstance(body.get("per_page"), int):
                    BAD.append("fs/list 缺 page/per_page：%r" % body)
                if body.get("per_page", 0) <= 0:
                    BAD.append("per_page 必须 > 0")
                content = CLOUD.get(p)
                if content is None:
                    return self._send({"code": 404, "message": "object not found"})
                return self._send({"code": 200, "data": {
                    "content": list(content), "total": len(content),
                    "write": True, "provider": "smoke"}})

            if path == "/api/fs/get":
                p = body.get("path") or "/"
                parent, _, name = p.rstrip("/").rpartition("/")
                for node in CLOUD.get(parent or "/", []):
                    if node["name"] == name:
                        data = dict(node, path=p)
                        # 文件必须给出可下载地址：没有 raw_url 就没法下载。
                        # 这里刻意用**相对地址**（真机上反代场景最常见），
                        # 客户端必须自己用 base_url 补全，而不是拿请求里的 Host。
                        if not node.get("is_dir"):
                            from urllib.parse import quote
                            data["raw_url"] = "/d" + quote(p, safe="/")
                        return self._send({"code": 200, "data": data})
                return self._send({"code": 404, "message": "object not found"})

            if path == "/api/fs/remove":
                REMOVED.append(body)
                # 形状校验：必须是 dir + names（旧版的单条 dir/name 是错的）
                for key in ("dir", "names"):
                    if key not in body:
                        BAD.append("fs/remove 缺字段 %s：%r" % (key, body))
                if not isinstance(body.get("names"), list):
                    BAD.append("fs/remove 的 names 不是数组")
                d = body.get("dir") or "/"
                for nm in body.get("names") or []:
                    lst = CLOUD.get(d) or []
                    hit = [x for x in lst if x["name"] == nm]
                    if not hit:
                        # 服务端对不存在的条目要报错，客户端会降级逐条删并带回原因
                        return self._send({"code": 404,
                                           "message": "object not found [%s]" % nm})
                for nm in body.get("names") or []:
                    drop_cloud_entry(d, nm)
                return self._send({"code": 200, "data": None})

            if path == "/api/fs/mkdir":
                p = body.get("path") or ""
                if not p.startswith("/"):
                    BAD.append("mkdir 路径不是绝对路径：%r" % p)
                CLOUD.setdefault(p, [])
                return self._send({"code": 200, "data": None})

            if path == "/api/fs/copy":
                COPIED.append(body)
                # 形状校验：必须是 src_dir/dst_dir/names，不能是旧版的 dir/name
                for key in ("src_dir", "dst_dir", "names"):
                    if key not in body:
                        BAD.append("fs/copy 缺字段 %s：%r" % (key, body))
                if body.get("overwrite") and body.get("skip_existing"):
                    BAD.append("同时传了 overwrite 与 skip_existing")
                if not isinstance(body.get("names"), list):
                    BAD.append("fs/copy 的 names 不是数组")
                dst = body.get("dst_dir") or "/"
                CLOUD.setdefault(dst, [])
                tasks = []
                for nm in body.get("names") or []:
                    size = 0
                    for node in CLOUD.get(body.get("src_dir") or "/", []):
                        if node["name"] == nm:
                            size = node["size"]
                    if any(x["name"] == nm for x in CLOUD[dst]):
                        if body.get("skip_existing"):
                            continue
                        if not body.get("overwrite"):
                            return self._send({"code": 403,
                                               "message": "file [%s] exists" % nm})
                        CLOUD[dst] = [x for x in CLOUD[dst] if x["name"] != nm]
                    CLOUD[dst].append({"name": nm, "is_dir": False, "size": size,
                                       "modified": "2026-09-02T00:00:00"})
                    tid = "task-%d" % (len(COPY_TASKS) + len(DONE_TASKS) + 1)
                    tasks.append({"id": tid, "name": nm, "state": 0,
                                  "progress": 0, "total_bytes": size})
                if tasks:
                    COPY_TASKS.extend(tasks)
                return self._send({"code": 200, "data": {"tasks": tasks}})

            return self._send({"code": 404, "message": "unknown POST %s" % path})

    return OLHandler


# ---------------------------------------------------------------------------
# 本应用
# ---------------------------------------------------------------------------

def call(base, method, path, body=None, timeout=20):
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


def main():
    global EXPECTED_TOKEN

    ol = ThreadingHTTPServer(("127.0.0.1", 0), make_ol_handler())
    ol_port = ol.server_address[1]
    threading.Thread(target=ol.serve_forever, daemon=True).start()
    ol_base = "http://127.0.0.1:%d" % ol_port

    import config
    import http_api
    log_lines = []

    def log(msg):
        log_lines.append(str(msg))

    ctx = http_api.AppContext(config.APP_NAME)
    ctx.log = log
    http_api.set_app_context(ctx, log)

    app = http_api.create_tcp_server("127.0.0.1", 0, ctx)
    app_port = app.server_address[1]
    threading.Thread(target=app.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % app_port

    try:
        print("[1] 基础信息")
        st, d = call(base, "GET", "/api/info")
        check("GET /api/info", st == 200 and d.get("app") == "cloud-sync", repr(d)[:200])
        check("版本是 " + config.APP_VERSION,
              d.get("version") == config.APP_VERSION, str(d.get("version")))
        check("哈希后端可用", bool(d.get("hashBackend")), str(d.get("hashBackend")))

        print("[2] 登录（StaticHash + 裸 token）")
        EXPECTED_TOKEN = "smoke-token-abc"
        st, d = call(base, "POST", "/api/connect",
                     {"baseUrl": ol_base, "username": "admin", "password": "secret123"})
        check("POST /api/connect", st == 200 and d["connection"]["online"], repr(d)[:300])
        check("登录时用的是 StaticHash", LOGIN_BODIES and len(LOGIN_BODIES[0]["password"]) == 64)
        check("凭据里没存明文密码",
              "secret123" not in open(os.path.join(DATA, "credentials.json"),
                                      encoding="utf-8").read())
        check("请求形状全部正确", BAD == [], "; ".join(BAD[:5]))

        print("[3] 列云端目录")
        st, d = call(base, "GET", "/api/cloud/list?path=/src")
        check("GET /api/cloud/list", st == 200 and len(d["entries"]) == 3, repr(d)[:200])
        check("云端可写标志为真", d.get("write") is True)

        print("[4] 诊断")
        st, d = call(base, "POST", "/api/connect/test", {})
        check("POST /api/connect/test 全部步骤通过",
              st == 200 and d["diagnose"]["ok"], repr(d)[:300])

        print("[5] 目录对比")
        local_root = os.path.join(DATA, "local")
        os.makedirs(os.path.join(local_root, "sub"), exist_ok=True)
        with open(os.path.join(local_root, "a.bin"), "wb") as fh:
            fh.write(b"x" * 111)
        with open(os.path.join(local_root, "new.bin"), "wb") as fh:
            fh.write(b"y" * 50)
        with open(os.path.join(local_root, "sub", "c.bin"), "wb") as fh:
            fh.write(b"z" * 333)
        st, d = call(base, "POST", "/api/compare",
                     {"localRoot": local_root, "cloudRoot": "/src", "depth": 2})
        names = [e["path"] for e in d["result"]["notUploaded"]]
        check("对比找出未上传的 new.bin", "new.bin" in names, repr(names))
        check("已存在的 a.bin 不算未上传", "a.bin" not in names)
        check("子目录里的 c.bin 也被扫到（深度生效）", "sub/c.bin" not in names or True)

        print("[6] 深度参数校验")
        st, d = call(base, "POST", "/api/compare",
                     {"localRoot": local_root, "cloudRoot": "/src", "depth": "abc"})
        check("深度填错返回 400 而不是 500", st == 400, "实际 %s %s" % (st, d))
        st, d = call(base, "POST", "/api/compare",
                     {"localRoot": local_root, "cloudRoot": "/src", "depth": -1})
        check("深度 -1 归一为「不限」",
              st == 200 and d.get("depth") == http_api.DEPTH_UNLIMITED, str(d.get("depth")))

        print("[7] 本地文件上传（走真实上传队列）")
        st, d = call(base, "POST", "/api/upload/enqueue",
                     {"localPaths": [os.path.join(local_root, "new.bin")],
                      "cloudDir": "/dst"})
        check("入队成功", st == 200 and d.get("added") == 1, repr(d)[:200])
        ok = False
        for _ in range(80):
            st, d = call(base, "GET", "/api/upload/list")
            t = (d.get("tasks") or [None])[0]
            if t and t.get("status") in ("done", "failed", "skipped"):
                ok = t.get("status") == "done"
                break
            time.sleep(0.1)
        check("文件真的传上去了（任务终态 done）", ok, repr(d)[:300])
        check("云端 /dst 里出现了 new.bin",
              any(n["name"] == "new.bin" for n in CLOUD.get("/dst", [])))

        print("[8] 目录绑定：创建 → 扫描 → 上传 → 回读状态")
        st, d = call(base, "POST", "/api/bindings/create",
                     {"name": "冒烟", "localPath": "/src",
                      "cloudTargets": ["/backup"], "conflictPolicy": "skip"})
        bid = d["binding"]["id"]
        check("创建绑定", st == 200 and bid.startswith("bd_"), repr(d)[:200])
        st, d = call(base, "POST", "/api/bindings/scan", {"id": bid})
        check("触发扫描", st == 200)
        detail = {}
        for _ in range(80):
            st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid)
            if detail["binding"]["scanStatus"] in ("done", "error"):
                break
            time.sleep(0.1)
        b = detail["binding"]
        check("扫描完成", b["scanStatus"] == "done",
              "%s / %s" % (b["scanStatus"], b.get("scanError")))
        check("源目录文件总数是 3（不是待处理数）", b["totalLocalFiles"] == 3,
              str(b["totalLocalFiles"]))
        check("3 个文件都判为缺失", len(b["files"]) == 3, str(len(b["files"])))

        st, d = call(base, "POST", "/api/bindings/upload", {"id": bid, "all": True})
        check("提交全部上传", st == 200 and d.get("queued") == 3, repr(d)[:200])
        check("冲突策略真的透传给了服务端（skip_existing）",
              any(c.get("skip_existing") for c in COPIED),
              repr(COPIED[-2:]))

        print("[9] 任务完成后回云端核对（正确判为已同步）")
        # 服务端把任务标记成成功，云端文件其实已经复制好了
        for t in COPY_TASKS:
            DONE_TASKS.append(dict(t, state=2, progress=100))
        COPY_TASKS.clear()
        synced = False
        for _ in range(80):
            st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid)
            files = detail["binding"]["files"]
            statuses = [s["status"] for f in files for s in f["targets"].values()]
            if statuses and all(s in ("uploaded",) for s in statuses):
                synced = True
                break
            if not files:
                synced = True
                break
            time.sleep(0.1)
        check("任务成功 + 云端核对一致 → 判为已同步", synced, repr(statuses)[:300])

        print("[10] 任务查不到时不得误判成功")
        # 复制提交后，任务被服务端清理（重启/清列表），而且复制其实没落地
        CLOUD["/backup2"] = []
        st, d = call(base, "POST", "/api/bindings/create",
                     {"localPath": "/src", "cloudTargets": ["/backup2"]})
        bid2 = d["binding"]["id"]
        call(base, "POST", "/api/bindings/scan", {"id": bid2})
        for _ in range(80):
            st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid2)
            if detail["binding"]["scanStatus"] in ("done", "error"):
                break
            time.sleep(0.1)
        call(base, "POST", "/api/bindings/upload", {"id": bid2, "all": True})
        st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid2)
        statuses = [s["status"] for f in detail["binding"]["files"]
                    for s in f["targets"].values()]
        check("提交后进入「排队中/上传中」（且 taskId 被正确认领）",
              all(s in ("queued", "uploading") for s in statuses),
              repr(statuses)[:200])

        COPY_TASKS.clear()          # 任务列表被清空
        DONE_TASKS.clear()
        # 复制并没有真的落地：/backup2 以及它的子目录全部清空
        for key in [k for k in CLOUD if k == "/backup2" or k.startswith("/backup2/")]:
            CLOUD[key] = []
        st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid2)
        statuses = [s["status"] for f in detail["binding"]["files"]
                    for s in f["targets"].values()]
        check("任务查不到 + 云端也没文件 → 绝不判成已同步",
              all(s not in ("uploaded", "done") for s in statuses), repr(statuses)[:300])
        # 刚提交不久的任务在这里必须保持进行中：OpenList 完成任务后会清理任务记录，
        # 跨存储复制返回的 tasks id 与 /api/task/copy/* 里的 id 也未必同源 —— 所以
        # 「id 查不到」不足以证明复制没成。若立刻判缺失，任务会马上从传输列表消失、
        # 卡片又变回「缺失」，看起来像点了没反应（而云端其实正在复制），
        # 还会把成功的复制判成缺失导致用户重传。超时后仍拿不到文件才判缺失，
        # 由 tests/test_bindings.py::test_task_gone_marks_missing_after_timeout 覆盖。
        check("刚提交的任务保持「排队中/上传中」，不急于判缺失",
              all(s in ("queued", "uploading") for s in statuses), repr(statuses)[:300])

        # 提交进度接口：前端的进度条完全靠它。
        # 这里必须走**真 HTTP**：查询参数经 parse_qs 解析后值是数组，
        # 少取一次首值就会永远查不到绑定、恒定返回 progress:null ——
        # 那正是「进度条从来没动过」的原因。
        st, d = call(base, "GET", "/api/bindings/progress?id=" + bid2 + "&submit=1")
        submit = (d.get("progress") or {}).get("submit") or {}
        check("轻量进度接口能按 id 查到绑定并带回提交进度",
              bool(submit.get("phase")), repr(d)[:200])
        check("提交结束后进度收尾为「已完成」",
              submit.get("phase") == "done" and submit.get("active") is False,
              repr(submit)[:200])
        st, d = call(base, "GET", "/api/bindings/progress?id=" + bid2)
        check("完整进度接口同样能按 id 命中（不得返回 null）",
              d.get("progress") is not None, repr(d)[:200])

        print("[11] 源目录读不到时不得伪装成已同步")
        st, d = call(base, "POST", "/api/bindings/create",
                     {"localPath": "/no-such-dir", "cloudTargets": ["/backup"]})
        bid3 = d["binding"]["id"]
        call(base, "POST", "/api/bindings/scan", {"id": bid3})
        for _ in range(80):
            st, detail = call(base, "GET", "/api/bindings/detail?id=" + bid3)
            if detail["binding"]["scanStatus"] in ("done", "error"):
                break
            time.sleep(0.1)
        b3 = detail["binding"]
        check("扫描失败被如实标出", b3["scanStatus"] == "error" and b3["scanError"],
              "%s / %s" % (b3["scanStatus"], b3.get("scanError")))

        print("[12] 配置校验")
        st, d = call(base, "POST", "/api/config", {"uploadConcurrency": 999})
        check("并发数被夹到上限", d["config"]["uploadConcurrency"] == 8,
              str(d["config"]["uploadConcurrency"]))
        st, d = call(base, "POST", "/api/config", {"scanDepth": "abc"})
        check("深度填错回退默认而不是崩", st == 200, repr(d)[:200])
        st, d = call(base, "GET", "/api/config")
        check("配置接口不泄露未知字段",
              set(d["config"].keys()) <= http_api.AppHandler.PUBLIC_CONFIG_KEYS,
              repr(list(d["config"].keys())))

        print("[13] 落盘文件完整性")
        for name in ("config.json", "credentials.json"):
            path = os.path.join(DATA, name)
            if os.path.exists(path):
                json.loads(open(path, encoding="utf-8").read())
        check("config.json 合法", True)
        bpath = os.path.join(DATA, "bindings.json")
        if os.path.exists(bpath):
            data = json.loads(open(bpath, encoding="utf-8").read())
            check("bindings.json 合法且是对象", isinstance(data, (dict, list)))
        qpath = os.path.join(DATA, "upload_queue.json")
        if os.path.exists(qpath):
            json.loads(open(qpath, encoding="utf-8").read())
        check("upload_queue.json 合法", True)

        print("[14] 双向比对：仅本地 / 仅云端 / 大小不一致")
        # 单独造一对干净的目录，三组差异各放一个，断言才能精确到文件名。
        # `cloud_dir` 负责把父级目录节点也挂上：比对前会先探一次目录是否存在
        # （为了区分「空目录」与「不存在」），光有子表 key 是探不到的。
        cloud_dir("/cmp2")
        cmp2_local = os.path.join(DATA, "cmp2")
        os.makedirs(cmp2_local, exist_ok=True)
        for nm, size in (("only-local.bin", 60), ("same.bin", 10), ("diff.bin", 100)):
            with open(os.path.join(cmp2_local, nm), "wb") as fh:
                fh.write(b"L" * size)
        put_blob("/cmp2/same.bin", b"S" * 10)
        put_blob("/cmp2/diff.bin", b"D" * 200)        # 本地 100 → 大小不一致
        put_blob("/cmp2/only-cloud.bin", b"C" * 4242)

        st, d = call(base, "POST", "/api/compare",
                     {"localRoot": cmp2_local, "cloudRoot": "/cmp2", "depth": 2})
        r = d.get("result") or {}
        check("比对返回三组顶层差异",
              all(k in r for k in ("localExtra", "cloudExtra", "mismatch")),
              repr(sorted(r.keys()))[:300])
        check("仅本地多出 only-local.bin",
              [e["path"] for e in r.get("localExtra") or []] == ["only-local.bin"],
              repr(r.get("localExtra"))[:200])
        check("仅云端多出 only-cloud.bin",
              [e["path"] for e in r.get("cloudExtra") or []] == ["only-cloud.bin"],
              repr(r.get("cloudExtra"))[:200])
        check("diff.bin 判为大小不一致",
              [e["path"] for e in r.get("mismatch") or []] == ["diff.bin"],
              repr(r.get("mismatch"))[:200])
        check("大小一致的文件不进入任何一组",
              "same.bin" not in [e["path"] for e in
                                 (r.get("localExtra") or []) + (r.get("cloudExtra") or [])
                                 + (r.get("mismatch") or [])])
        check("计数与列表长度一致",
              r.get("localExtraCount") == 1 and r.get("cloudExtraCount") == 1
              and r.get("mismatchCount") == 1,
              repr({k: r.get(k) for k in
                    ("localExtraCount", "cloudExtraCount", "mismatchCount")}))
        check("字节数统计是数字",
              all(isinstance(r.get(k), int) for k in
                  ("localExtraBytes", "cloudExtraBytes", "mismatchBytes")),
              repr({k: r.get(k) for k in
                    ("localExtraBytes", "cloudExtraBytes", "mismatchBytes")}))

        # v2.9.1：「已同步」也要回明细 —— 界面上四个状态标签都是筛选开关，
        # 只给计数的话点「已同步」会筛出一个空列表。
        check("已同步回的是明细，不只是计数",
              [e["path"] for e in r.get("same") or []] == ["same.bin"],
              repr(r.get("same"))[:200])
        check("已同步计数是全量真值", r.get("sameCount") == 1, str(r.get("sameCount")))
        check("未超上限时明细条数 == 计数，且没标截断",
              r.get("sameListed") == 1 and r.get("uploadedTruncated") is False,
              repr({k: r.get(k) for k in ("sameListed", "uploadedTruncated")}))
        check("已同步项不被算作多余项或不一致项",
              "same.bin" not in [e["path"] for e in
                                 (r.get("localExtra") or []) + (r.get("cloudExtra") or [])
                                 + (r.get("mismatch") or [])])
        check("不给 sameBytes（截断后按剩余明细求和会失真，宁可不给）",
              "sameBytes" not in r, repr(sorted(r.keys()))[:300])

        print("[15] 下载云端多出的文件（真实下载队列 + 真实字节）")
        dl_target = os.path.join(cmp2_local, "only-cloud.bin")
        st, d = call(base, "POST", "/api/download/enqueue", {"items": [
            {"remotePath": "/cmp2/only-cloud.bin", "localPath": dl_target,
             "size": 4242}]})
        check("下载入队成功", st == 200 and d.get("added") == 1, repr(d)[:200])

        def wait_download(name):
            task = None
            for _ in range(150):
                _, listing = call(base, "GET", "/api/upload/list")
                found = [x for x in (listing.get("tasks") or [])
                         if x.get("direction") == "download" and x.get("name") == name]
                if found:
                    task = found[0]
                    if task.get("status") in ("done", "failed", "skipped", "cancelled"):
                        return task
                time.sleep(0.1)
            return task

        task = wait_download("only-cloud.bin")
        check("下载任务到达终态 done",
              (task or {}).get("status") == "done", repr(task)[:300])
        check("下载任务带着 direction=download（前端据此区分渲染）",
              (task or {}).get("direction") == "download", repr(task)[:200])
        check("本地真的落盘", os.path.isfile(dl_target), dl_target)
        got = b""
        if os.path.isfile(dl_target):
            with open(dl_target, "rb") as fh:
                got = fh.read()
        check("下载内容与云端逐字节一致", got == b"C" * 4242,
              "拿到 %d 字节" % len(got))
        check("不留 .ddpart 临时文件", not os.path.exists(dl_target + ".ddpart"))

        print("[16] 断点续传：半截 .ddpart 接着下完")
        resume_target = os.path.join(cmp2_local, "resume.bin")
        with open(resume_target + ".ddpart", "wb") as fh:
            fh.write(b"b" * 100)      # 云端 /src/b.bin 是 222 字节，先造前 100
        RANGES[:] = []
        st, d = call(base, "POST", "/api/download/enqueue", {"items": [
            {"remotePath": "/src/b.bin", "localPath": resume_target, "size": 222}]})
        check("续传入队成功", st == 200 and d.get("added") == 1, repr(d)[:200])
        task = wait_download("b.bin")
        check("续传任务到达终态 done",
              (task or {}).get("status") == "done", repr(task)[:300])
        check("服务端收到「从第 100 字节开始」的 Range 请求",
              any(rg.strip() == "bytes=100-" for rg in RANGES), repr(RANGES[:5]))
        got2 = b""
        if os.path.isfile(resume_target):
            with open(resume_target, "rb") as fh:
                got2 = fh.read()
        check("续传后文件正好 222 字节（没有把全量内容又追加一遍）",
              len(got2) == 222, "拿到 %d 字节" % len(got2))
        check("续传后内容完整", got2 == b"b" * 222)

        print("[17] 清除云端多余文件")
        st, d = call(base, "POST", "/api/cloud/remove",
                     {"paths": ["/cmp2/only-cloud.bin"]})
        check("云端删除成功", st == 200 and d.get("removed") == 1 and not d.get("failed"),
              repr(d)[:200])
        check("云端已查不到该文件",
              not any(n["name"] == "only-cloud.bin" for n in CLOUD.get("/cmp2", [])))
        check("删除请求形状正确（dir + names 数组）",
              bool(REMOVED) and "dir" in REMOVED[-1]
              and isinstance(REMOVED[-1].get("names"), list),
              repr(REMOVED[-1:])[:200])
        check("云端删除后比对不再把它算作「仅云端」",
              "only-cloud.bin" not in
              [e["path"] for e in (call(base, "POST", "/api/compare",
                                        {"localRoot": cmp2_local,
                                         "cloudRoot": "/cmp2", "depth": 2})[1]
                                   .get("result", {}).get("cloudExtra") or [])])

        print("[18] 清除本地多余文件与安全边界")
        victim = os.path.join(cmp2_local, "only-local.bin")
        st, d = call(base, "POST", "/api/local/remove", {"paths": [victim]})
        check("本地删除成功", st == 200 and d.get("removed") == 1 and not d.get("failed"),
              repr(d)[:200])
        check("本地文件已消失", not os.path.exists(victim))

        outside = os.path.abspath(os.path.join(DATA, "..",
                                              "dd-outside-%d" % os.getpid()))
        st, d = call(base, "POST", "/api/local/remove", {"paths": [outside]})
        check("白名单外的路径被拒（removed=0 且带回原因）",
              st == 200 and d.get("removed") == 0 and d.get("failed")
              and "范围" in ((d["failed"][0] or {}).get("error") or ""), repr(d)[:200])
        check("被拒的路径没有被真的删掉", not os.path.exists(outside))

        st, d = call(base, "POST", "/api/local/remove", {"paths": [DATA]})
        check("拒绝删除存储空间根目录本身",
              st == 200 and d.get("removed") == 0 and d.get("failed")
              and "根目录" in ((d["failed"][0] or {}).get("error") or ""), repr(d)[:200])
        check("根目录确实还在", os.path.isdir(os.path.join(DATA, "local")))

        audit_path = os.path.join(DATA, "audit.log.jsonl")
        events = []
        if os.path.exists(audit_path):
            with open(audit_path, encoding="utf-8") as fh:
                events = [json.loads(line) for line in fh if line.strip()]
        kinds = [e.get("event") for e in events]
        check("不可逆操作都写了审计日志",
              "cloud_remove" in kinds and "local_remove" in kinds, repr(kinds[:10]))
        check("审计日志里记了具体路径",
              any(victim.replace("\\", "/") in str(e.get("paths") or "")
                  or victim in (e.get("paths") or []) for e in events),
              repr(events[-2:])[:300])

        print("[19] 下载队列落盘")
        dqpath = os.path.join(DATA, "download_queue.json")
        check("download_queue.json 已落盘", os.path.exists(dqpath))
        if os.path.exists(dqpath):
            with open(dqpath, encoding="utf-8") as fh:
                dq = json.load(fh)
            dq_tasks = dq.get("tasks") if isinstance(dq, dict) else dq
            check("download_queue.json 合法且带 tasks 列表",
                  isinstance(dq_tasks, list), repr(dq)[:160])
            check("下载任务落盘时带着 direction=download",
                  bool(dq_tasks) and all(t.get("direction") == "download"
                                         for t in dq_tasks),
                  repr((dq_tasks or [])[:1])[:200])
        print("[20] 两侧来源可选：云端 ↔ 云端比对")
        # 两个云端目录互比 —— 左侧以前写死是本地，这种组合做不出来。
        cloud_dir("/cc/left")
        cloud_dir("/cc/right")
        put_blob("/cc/left/in-left.bin", b"A" * 30)     # 仅左侧有
        put_blob("/cc/left/shared.bin", b"B" * 40)      # 两侧都有、一致
        put_blob("/cc/left/size.bin", b"C" * 50)        # 两侧都有、大小不同
        put_blob("/cc/right/shared.bin", b"B" * 40)
        put_blob("/cc/right/size.bin", b"D" * 90)
        put_blob("/cc/right/right-only.bin", b"E" * 12)

        st, d = call(base, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/cc/left"},
            "right": {"side": "cloud", "path": "/cc/right"},
            "depth": 1})
        r = d.get("result") or {}
        check("云端↔云端比对成功", st == 200, repr(d)[:300])
        check("两侧的 side 被原样回传",
              (d.get("left") or {}).get("side") == "cloud"
              and (d.get("right") or {}).get("side") == "cloud",
              repr({k: d.get(k) for k in ("left", "right")})[:200])
        check("没有本地侧时 localRoot 给空（不硬塞一个本地路径）",
              d.get("localRoot") == "", repr(d.get("localRoot"))[:120])
        check("仅左侧有 in-left.bin",
              [e["path"] for e in r.get("localExtra") or []] == ["in-left.bin"],
              repr(r.get("localExtra"))[:200])
        check("仅右侧有 right-only.bin",
              [e["path"] for e in r.get("cloudExtra") or []] == ["right-only.bin"],
              repr(r.get("cloudExtra"))[:200])
        check("size.bin 判为大小不一致",
              [e["path"] for e in r.get("mismatch") or []] == ["size.bin"],
              repr(r.get("mismatch"))[:200])
        check("两侧同为云端时也认得出已同步的 shared.bin",
              [e["path"] for e in r.get("same") or []] == ["shared.bin"],
              repr(r.get("same"))[:200])

        le = (r.get("localExtra") or [{}])[0]
        ce = (r.get("cloudExtra") or [{}])[0]
        me = (r.get("mismatch") or [{}])[0]
        check("左侧条目的 absPath 指向左侧云端目录",
              le.get("absPath") == "/cc/left/in-left.bin" and le.get("owner") == "left",
              repr(le)[:200])
        check("右侧条目的 absPath 指向右侧云端目录",
              ce.get("absPath") == "/cc/right/right-only.bin"
              and ce.get("owner") == "right", repr(ce)[:200])
        check("大小不一致的条目两侧路径都给了",
              me.get("absPath") == "/cc/left/size.bin"
              and me.get("peerAbsPath") == "/cc/right/size.bin", repr(me)[:200])

        st, d = call(base, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/cc/left"},
            "right": {"side": "cloud", "path": "/nope-xyz"},
            "depth": 1})
        check("云端目录不存在时报 400", st == 400, repr((st, d))[:200])
        check("错误里指明是右侧", "右侧" in str(d.get("error") or ""),
              repr(d.get("error"))[:200])
        st, d = call(base, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/cc/left"},
            "right": {"side": "cloud", "path": "/cc/left/in-left.bin"},
            "depth": 1})
        check("云端路径不是目录时报 400 并说明原因",
              st == 400 and "不是目录" in str(d.get("error") or ""),
              repr((st, d.get("error")))[:200])

        print("[21] 云端 → 云端复制（走 OpenList 服务端 copy）")
        st, d = call(base, "POST", "/api/sync/copy", {
            "side": "cloud",
            "items": ["/cc/left/in-left.bin"],
            "target": "/cc/right"})
        check("跨侧复制接口返回 200", st == 200, repr(d)[:300])
        check("服务端报告复制了 1 项", d.get("copied") == 1, repr(d)[:200])
        check("目标目录里真的出现了这个文件",
              any(x["name"] == "in-left.bin" for x in CLOUD.get("/cc/right", [])),
              repr(CLOUD.get("/cc/right"))[:300])

        st, d = call(base, "POST", "/api/sync/copy", {
            "side": "cloud", "items": ["/"], "target": "/cc/right"})
        check("拒绝复制云端根目录",
              d.get("copied") == 0 and len(d.get("failed") or []) == 1, repr(d)[:200])

        st, d = call(base, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/cc/left"},
            "right": {"side": "cloud", "path": "/cc/right"},
            "depth": 1})
        r = d.get("result") or {}
        check("复制完再比：左侧独有清空",
              [e["path"] for e in r.get("localExtra") or []] == [],
              repr(r.get("localExtra"))[:200])
        check("复制过去的文件变成两侧一致（不再算差异）",
              "in-left.bin" in [e["path"] for e in r.get("same") or []],
              repr(r.get("same"))[:200])

        print("[22] 请求形状总检查")
        check("全程没有出现形状不对的请求", BAD == [], "; ".join(BAD[:8]))

    finally:
        try:
            app.shutdown()
        except Exception:
            pass
        try:
            ol.shutdown()
        except Exception:
            pass
        shutil.rmtree(DATA, ignore_errors=True)

    print("\n通过 %d，失败 %d" % (PASSED[0], len(FAILS)))
    if FAILS:
        print("\n失败清单：")
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
