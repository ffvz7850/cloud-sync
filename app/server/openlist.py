# -*- coding: utf-8 -*-
"""OpenList API 客户端。

契约全部来自 `docs/API参考.md`（OpenList v4.2.6 server 源码逐行核对）。要点：

* 登录：`POST /api/auth/login/hash`，密码先算 StaticHash（sha256(密码+固定盐)）。
  存下来的是 **StaticHash**（密码等价物，但不是明文），用于 token 失效后自动重登。
* token 走 `Authorization: <token>` 裸值 header，遇到 401 自动用存的凭据重登再重试。
* 上传：小文件 `fs/put` 同步直传（流式分块，报进度）；大文件优先 `fs/multipart`
  分片（断点续传），init 报 403「disabled」自动降级回 `fs/put`。
* 所有网络与 OpenList 的交互都收敛在这一个文件 —— 出问题改这一处。
"""

import os
import threading
import time
from urllib.parse import quote

import config
import hasher
import httpclient

# StaticHash 的固定盐。注意：OpenList 为兼容沿用 alist 的旧盐，不是 openlist。
STATIC_HASH_SALT = "https://github.com/alist-org/alist"

DEFAULT_PER_PAGE = 200
# 自适应分页的上限：第一页拿到的 total 不超过它，就改用 total 一次要全。
# 再大就退回普通翻页 —— 一次几万个条目的响应体对 NAS 和 OpenList 都不友好。
MAX_PER_PAGE = 10000
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024   # 10MB，与服务端默认上限一致
SMALL_FILE_THRESHOLD = 4 * 1024 * 1024  # 小于这个走同步直传，省得分片开销

# OpenList 后台任务状态（pkg/tache/state.go）。只在判断复制任务结果时用。
TASK_STATE_SUCCEEDED = 2
TASK_STATE_FAILED = (4, 7)      # 4=已取消，7=失败且无剩余重试次数
# 「失败、还能重试」的一组状态，专门给传输列表的「全部重试」计数用：
# 5=errored（出错待重试）、6=failing（失败收尾）、7=failed（重试次数耗尽）。
TASK_STATES_FAILED = (5, 6, 7)

# 传输列表要覆盖的任务类型。OpenList 的任务端点形状是 /api/task/<type>/<action>：
#   copy   —— 目录绑定备份、文件对比的跨存储复制产生；
#   upload —— `As-Task` 模式的上传产生。
# v2.15.39 及以前只处理 copy，于是 upload 类型任务在「全部取消」里永远停不下来。
TASK_TYPES = ("copy", "upload")

TASK_ACTION_LABELS = {"cancel": "取消", "retry": "重试", "delete": "删除"}


def normalize_tids(tids):
    """把任务 id 清单收敛成去空白、去重、保序的字符串列表。"""
    out = []
    seen = set()
    for raw in tids or []:
        tid = str(raw or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def parse_batch_errors(data):
    """解析批量任务接口返回的 `data`，得到 `{tid: 错误原因}`。

    **这是「全部取消不生效」的根因所在**：真实 OpenList（server/handles/task.go
    的 `getBatchHandler`）对批量接口**无论 tid 是否存在都返回 HTTP 200 +
    code 200**，每个 tid 的结果放在 `data` 里 —— 成功是 null（Go 的 nil error），
    失败是一句原因（`"task not found"` 之类）。所以「HTTP 成功」绝不等于
    「任务被取消了」，必须逐条看这张表。

    返回 None 表示**无法判断**（data 不是字典，或是个空字典 —— 服务端什么都没
    告诉我们）。调用方必须把 None 当成「不知道」，绝不能当成成功 —— 旧实现
    正是在这里 `return len(tids)` 谎报「已取消 N 个」。
    """
    if isinstance(data, dict) and data:
        errors = {}
        for key, value in data.items():
            if value is None or value is False or value == "" or value == 0:
                continue
            errors[str(key)] = str(value)
        return errors
    return None


def count_failed_tasks(tasks):
    """数出任务列表里「失败」状态的条数（state 5/6/7）。

    `retry_failed` 接口**不返回重试条数**，只能靠调用前后各数一次做差 ——
    所以这个函数只是那套"差量核算"的一半，别指望它给出精确的重试数。
    """
    count = 0
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        state = task.get("state")
        if state is None:
            state = TASK_STATE_SUCCEEDED
        try:
            value = int(state)
        except (TypeError, ValueError):
            continue
        if value in TASK_STATES_FAILED:
            count += 1
    return count


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------

class OpenlistError(Exception):
    """业务错误（OpenList 返回 code != 200，或语义失败）。"""

    def __init__(self, message, code=None, status=None, kind="business"):
        Exception.__init__(self, message)
        self.code = code
        self.status = status
        self.kind = kind      # business | auth | network


class AuthError(OpenlistError):
    def __init__(self, message, code=401, status=401):
        OpenlistError.__init__(self, message, code=code, status=status, kind="auth")


class NetworkError(OpenlistError):
    def __init__(self, message, status=None):
        OpenlistError.__init__(self, message, code=None, status=status, kind="network")


def static_hash(password):
    """OpenList 登录用的密码哈希：sha256_hex(密码 + '-' + 固定盐)。"""
    return hasher.sha256_hex("%s-%s" % (password, STATIC_HASH_SALT))


def normalize_base_url(raw):
    """把用户随手填的地址收敛成 http://host:port。

    接受：192.168.0.10:5244 / http://192.168.0.10:5244 / http://host:5244/
         / https://... / http://host:5244/api/fs/xxx（任何路径后缀都会被剥掉）
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    # 只保留 scheme://host[:port]，剥掉任何路径部分
    # （用户可能把浏览器整段 URL 粘进来，路径会导致 /api/fs/... 拼错）
    from urllib.parse import urlsplit
    parts = urlsplit(raw)
    netloc = parts.netloc
    if not netloc:
        return raw.rstrip("/")
    return "%s://%s" % (parts.scheme or "http", netloc)


# --------------------------------------------------------------------------
# 凭据存储
# --------------------------------------------------------------------------

class CredentialStore(object):
    """凭据存取（base_url / username / passwordHash / token），落盘为 JSON。

    刻意**不存明文密码**：存的是 StaticHash。这样凭据文件泄露时攻击者拿到的
    是一个「能登录但还原不出明文」的等价物。
    """

    def __init__(self, path=None):
        self.path = path or config.credentials_path()
        self._lock = threading.Lock()

    def load(self):
        # 走 config 的带缓存读取：凭据文件很小，但**每次 OpenList 请求都要读它**，
        # 云端扫描动辄上千次请求，省下的是上千次「打开 + 解析 JSON」。
        data = config.read_cached_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def save(self, data):
        with self._lock:
            data = dict(data)
            data["savedAt"] = int(time.time())
            config.atomic_write_json(self.path, data)
        return data

    def patch(self, **kv):
        data = self.load()
        data.update(kv)
        return self.save(data)

    def clear(self):
        with self._lock:
            try:
                os.unlink(self.path)
            except Exception:
                pass
            config.invalidate_file_cache(self.path)

    # -- 字段便捷访问 --
    def base_url(self):
        return self.load().get("baseUrl", "")

    def username(self):
        return self.load().get("username", "")

    def password_hash(self):
        return self.load().get("passwordHash", "")

    def token(self):
        return self.load().get("token", "")

    def is_configured(self):
        data = self.load()
        return bool(data.get("baseUrl") and data.get("username")
                    and (data.get("passwordHash") or data.get("token")))

    def status(self):
        data = self.load()
        return {
            "configured": self.is_configured(),
            "baseUrl": data.get("baseUrl", ""),
            "username": data.get("username", ""),
            "hasToken": bool(data.get("token")),
            "savedAt": data.get("savedAt", 0),
        }


# --------------------------------------------------------------------------
# 目录缓存
# --------------------------------------------------------------------------

class DirCache(object):
    """一次批量操作内共享的「云端目录已确认存在」缓存（线程安全）。

    为什么需要：一次「一键上传」会对**每个子目录**各调一次 `ensure_dir`，
    而这些子目录共享同样的父级路径；并发 worker 之间又会各查一遍。实测提交
    30 个子目录 / 120 个文件时，`fs/get` 发了 90 次，其中 54 次（60%）是纯
    重复的父级路径 —— 这些请求全部是白付的网络往返。

    生命周期 = 一次批量提交，**刻意不跨请求保留**：用户随时可能在 OpenList
    那边删掉目录，缓存活太久会让 copy 打到一个已经不存在的目录上。
    """

    def __init__(self):
        self._ok = set()
        self._locks = {}
        self._guard = threading.Lock()
        self.hits = 0        # 被缓存挡掉的重复查询次数（诊断与测试用）

    def known(self, path):
        with self._guard:
            hit = path in self._ok
            if hit:
                self.hits += 1
        return hit

    def mark(self, path):
        with self._guard:
            self._ok.add(path)

    def lock_for(self, path):
        """每个路径一把锁：同一个目录被多个 worker 同时 ensure 时只做一次。

        没有它，8 路并发会在同一个窗口里各自 `stat` + `mkdir` 同一批目录
        （实测 36 个唯一目录被 mkdir 了 57 次，多出来的全是并发重复）。
        """
        with self._guard:
            lk = self._locks.get(path)
            if lk is None:
                lk = threading.Lock()
                self._locks[path] = lk
            return lk


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class OpenlistClient(object):
    def __init__(self, store, log=None):
        self.store = store
        self.log = log or (lambda *a, **k: None)
        self._reauth_lock = threading.Lock()

    # -- 基础 --

    def _log(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def base_url(self):
        base = self.store.base_url()
        if not base:
            raise OpenlistError("尚未配置 OpenList 地址，请先在「连接」里填写", kind="business")
        return base

    def is_configured(self):
        return self.store.is_configured()

    # -- 登录 --

    def login(self, base_url, username, password, otp_code=""):
        """用地址 + 用户名 + 明文密码登录。存 StaticHash（不落明文）与 token。"""
        base = normalize_base_url(base_url)
        if not base:
            raise OpenlistError("OpenList 地址不能为空")
        username = (username or "").strip()
        if not username:
            raise OpenlistError("用户名不能为空")
        pwd_hash = static_hash(password or "")

        payload = {"username": username, "password": pwd_hash, "otp_code": otp_code or ""}
        # 注意：此时 store 里还没有 baseUrl（首次登录），必须显式传 base_override，
        # 否则 _raw_request 会拿空地址拼出相对 URL，报 "unknown url type"
        data = self._raw_request("POST", "/api/auth/login/hash", json_body=payload,
                                 with_token=False, base_override=base)
        token = (data or {}).get("token") or ""
        if not token:
            raise OpenlistError("登录成功但服务端没有返回 token")
        self.store.save({
            "baseUrl": base,
            "username": username,
            "passwordHash": pwd_hash,
            "token": token,
        })
        self._log("已登录 OpenList：%s（用户 %s）" % (base, username))
        return self.store.status()

    def logout(self):
        self.store.clear()

    def _relogin(self):
        """token 失效后用存的凭据重新登录。返回是否成功。"""
        with self._reauth_lock:
            data = self.store.load()
            username = data.get("username", "")
            pwd_hash = data.get("passwordHash", "")
            if not username or not pwd_hash:
                return False
            try:
                payload = {"username": username, "password": pwd_hash, "otp_code": ""}
                result = self._raw_request("POST", "/api/auth/login/hash",
                                           json_body=payload, with_token=False)
                token = (result or {}).get("token") or ""
                if not token:
                    return False
                self.store.patch(token=token)
                self._log("token 已自动续期（用户 %s）" % username)
                return True
            except OpenlistError as exc:
                self._log("自动重登失败：%s" % exc)
                return False

    # -- HTTP 底层 --

    def _raw_request(self, method, api_path, query=None, json_body=None,
                     headers=None, with_token=True, timeout=60, base_override=None):
        """发一次请求并解析统一响应壳。不做 token 续期（那是 _request 的事）。

        base_override：首次登录时 store 里还没有地址，由 login() 显式传入。
        """
        import json as _json
        # 凭据只读一次。原来走 base_url() + token() 各读一遍文件，等于每个请求
        # 两次 IO；云端扫描是「每目录一次请求」，几百个目录就白读上千次。
        creds = self.store.load()
        # 没配地址就发请求会得到 "unknown url type" 这种谁也看不懂的错，
        # 这里给与 base_url() 完全一致的友好提示
        base = base_override or (creds.get("baseUrl") or "").strip()
        if not base:
            raise OpenlistError("尚未配置 OpenList 地址，请先在「连接」里填写", kind="business")
        url = base + api_path
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query)

        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        if with_token:
            token = creds.get("token") or ""
            if token:
                req_headers["Authorization"] = token

        body = None
        if json_body is not None:
            body = _json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        try:
            resp = httpclient.request(method, url, headers=req_headers, body=body,
                                      timeout=timeout)
        except httpclient.TransportError as exc:
            raise NetworkError("连不上 OpenList：%s" % exc, status=exc.status)

        try:
            raw = resp.read_all()
        finally:
            resp.close()

        status = resp.status
        try:
            payload = _json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            payload = {}

        if not isinstance(payload, dict) or "code" not in payload:
            # 不是 OpenList 的标准响应（可能是网关/反代的错误页）
            if status == 401:
                raise AuthError("未授权（401）", status=status)
            if status >= 400:
                raise OpenlistError("HTTP %s：%s" % (status, raw[:200].decode("utf-8", "replace")),
                                    status=status, kind="business")
            # 2xx 但不是 OpenList 标准响应（反代错误页、登录页等）→ 不能当成成功返回 {}，
            # 否则 list_dir 拿到 {content:[],total:0} 会把目录判为空，触发全量重传。
            # 变量名是 api_path（形参），这里曾写成 path —— 那个名字在函数里并不存在，
            # 于是一旦走到这条分支就抛 NameError，被上层兜成 HTTP 500「服务器内部错误」，
            # 注释里承诺的"报错说清原因"从来没生效过。
            # status 0（curl 后端拿不到状态行）与 3xx（没跟随后仍返回）同样属于"不是正常响应"。
            if api_path.startswith("/api/") or status <= 0 or 300 <= status < 400:
                raise OpenlistError("非 OpenList 响应（HTTP %s，缺少 code 字段）：%s"
                                    % (status, raw[:200].decode("utf-8", "replace")),
                                    status=status, kind="business")
            return payload

        code = payload.get("code")
        if code == 200:
            return payload.get("data")

        message = payload.get("message") or ("code=%s" % code)
        if code == 401 or status == 401:
            raise AuthError(message, code=code, status=status or 401)
        if code == 402 or status == 402:
            raise OpenlistError("需要两步验证码（2FA）：%s" % message,
                                code=code, status=status, kind="business")
        raise OpenlistError(message, code=code, status=status, kind="business")

    def _request(self, method, api_path, query=None, json_body=None, headers=None,
                 timeout=60, _retried=False):
        """带 token 续期的业务请求：401 时自动重登一次再重试。"""
        try:
            return self._raw_request(method, api_path, query=query, json_body=json_body,
                                     headers=headers, timeout=timeout)
        except AuthError:
            if _retried:
                raise AuthError("登录态失效，且自动重登也失败，请重新登录")
            if not self._relogin():
                raise AuthError("登录态失效，请重新登录")
            return self._raw_request(method, api_path, query=query, json_body=json_body,
                                     headers=headers, timeout=timeout)

    # -- 用户 / 目录 --

    def me(self):
        return self._request("GET", "/api/me")

    def list_dir(self, path, page=1, per_page=DEFAULT_PER_PAGE):
        data = self._request("POST", "/api/fs/list", json_body={
            "path": path or "/", "password": "", "refresh": False,
            "page": page, "per_page": per_page,
        })
        data = data or {}
        return {
            "content": data.get("content") or [],
            "total": int(data.get("total") or 0),
            "write": bool(data.get("write")),
            "provider": data.get("provider", ""),
        }

    def list_all(self, path, meta=None):
        """翻页拿全一个目录的直接子项。

        meta: 可选的 dict。传入时会把第一页的 `write` / `provider` 写进去，
        调用方就不必为了拿这两个字段再单独发一次 `fs/list`。

        **分页策略**：服务端（或中间网关）可能把 per_page 夹到自身上限（实测某些
        部署只给 300/页，甚至 100/页）。如果请求的 per_page > 服务端上限，offset
        会按请求值跳，导致中间条目被跳过。所以第一页先探测服务端真实上限：
        若返回条数 < 请求的 per_page 且 total 更大，就用返回条数作为新的
        per_page 重新从第一页开始。探测成功后 `len(content) < per_page` 才是
        可靠的「到尾部」判断。
        """
        out = []
        page = 1
        per_page = DEFAULT_PER_PAGE
        first = None
        probed = False
        while True:
            data = self.list_dir(path, page=page, per_page=per_page)
            if first is None:
                first = data
            content = data["content"] or []
            total = int(data["total"] or 0)

            # 第一页探测：服务端返回比请求少 → 有上限，用实际返回数重跑
            if (not probed and page == 1 and len(content) < per_page
                    and len(content) > 0 and total > len(content)):
                probed = True
                per_page = len(content)
                page = 1
                out = []
                first = data    # 保留第一页的 meta（write/provider）
                continue
            probed = True

            out.extend(content)
            if len(out) >= total or not content:
                break
            # 本页没拿满 → 服务端已到尾部，再翻也是空。
            # 经过探测后 per_page <= 服务端上限，这个判断是可靠的。
            if len(content) < per_page:
                break
            page += 1
            if page > 10000:      # 防御：异常循环
                self._log("list_all 翻页超过 10000 次，path=%s total=%s" % (path, total))
                break
        # 防御：理论上不该出现「已收 < total 但循环结束」，出现了说明服务端分页异常
        if total and len(out) < total:
            self._log("list_all 可能漏条目：path=%s 拿到 %d/%d" % (path, len(out), total))
        if meta is not None and first is not None:
            meta["write"] = first.get("write", False)
            meta["provider"] = first.get("provider", "")
        return out

    def get(self, path):
        return self._request("POST", "/api/fs/get", json_body={
            "path": path, "password": "",
        })

    def stat(self, path):
        """拿到文件/目录信息；不存在返回 None，不抛错。"""
        try:
            return self.get(path)
        except OpenlistError as exc:
            msg = str(exc).lower()
            # OpenList 的业务错误通常走 HTTP 200 + body{"code":404}，
            # 所以 exc.status 是 HTTP 状态码（多为 200），必须同时看 exc.code。
            not_found = (exc.code == 404 or exc.status == 404
                         or "not found" in msg or "object not found" in msg
                         or "不存在" in str(exc) or "no such" in msg
                         or "cannot find" in msg or "无此" in str(exc))
            if not_found:
                return None
            raise

    def exists(self, path):
        return self.stat(path) is not None

    def mkdir(self, path):
        return self._request("POST", "/api/fs/mkdir", json_body={"path": path})

    def ensure_dir(self, path, cache=None):
        """确保云端目录存在。**目录已经存在时只发一次请求。**

        `cache` 传 `DirCache` 时，同一批操作内的重复路径会被跳过（并发安全）；
        不传则本次调用内临时建一个 —— 「先查目标本身」的优化对所有调用点都生效。

        旧实现（v2.8.8 及以前）对路径的**每一级**都发一次 `fs/get`，而且并发
        worker 之间完全不共享结果：实测提交 30 个子目录 / 120 个文件时发了
        156~177 次请求，其中 54 次 stat + 18 次 mkdir 是纯重复。而 OpenList 网页
        上手动复制同一个文件夹只需 1 次请求 —— 「软件比 OpenList 还慢」的观感
        主要就来自这里。

        实现要点：**逐级从浅到深，每级各自加锁**。加锁顺序恒定由浅到深，且
        每级用完立刻释放（不是嵌套持锁），所以两个 worker 不可能各持一把锁
        互等 —— 这一点很关键：早期版本只在**目标路径**上加锁、中间层级裸奔，
        结果 5 个 worker 会同时 mkdir 同一个 `/backup`。
        """
        path = (path or "/").strip()
        if not path or path == "/":
            return
        cache = cache if cache is not None else DirCache()
        if cache.known(path):
            return
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            if cache.known(current):
                continue
            with cache.lock_for(current):
                # 等到锁之后再确认一次：可能刚被并发的另一个 worker 建好
                if cache.known(current):
                    continue
                if self._path_is_dir(current):
                    cache.mark(current)
                    continue
                try:
                    self.mkdir(current)
                except OpenlistError as exc:
                    # 并发下可能已被别的请求建出来
                    node = self.stat(current)
                    if node is None:
                        raise OpenlistError("创建云端目录失败（%s）：%s" % (current, exc))
                    if not node.get("is_dir"):
                        raise OpenlistError("云端路径已存在且不是目录：%s" % current)
                cache.mark(current)

    def _path_is_dir(self, path):
        """云端这个路径是不是目录。

        不存在返回 False（交给调用方去建）；存在但不是目录则报错 —— 与旧实现
        逐级检查时的语义一致，不能因为优化把「路径被同名文件占住」吞掉。
        """
        node = self.stat(path)
        if node is None:
            return False
        if not node.get("is_dir"):
            raise OpenlistError("云端路径已存在且不是目录：%s" % path)
        return True

    def remove(self, dir_path, names):
        return self._request("POST", "/api/fs/remove", json_body={
            "dir": dir_path, "names": list(names),
        })

    def copy(self, src_dir, dst_dir, names, overwrite=False, skip_existing=False,
             merge=False):
        """OpenList 服务端复制：把 src_dir 下的 names 复制到 dst_dir。

        这三个开关直接对应服务端 `MoveCopyReq`（server/handles/fsmanage.go）：
        * `skip_existing=True` —— 目标已存在就静默跳过，不报 403；
        * `overwrite=True`    —— 目标已存在直接覆盖；
        * 两者都不传          —— 已存在时报 403 `file [x] exists`（冲突即报错）。
        `merge=True` 走目录合并（fs.Merge），本应用不用。

        返回服务端的 `data`。同存储内复制是**同步**完成的（服务端不建任务，
        data 里没有 tasks）；跨存储才会各自建一个后台任务，此时
        `data["tasks"]` 是本批产生的真实任务列表（含 id / name / state）。
        """
        body = {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": list(names),
        }
        if overwrite:
            body["overwrite"] = True
        if skip_existing:
            body["skip_existing"] = True
        if merge:
            body["merge"] = True
        return self._request("POST", "/api/fs/copy", json_body=body)

    def list_tasks(self, task_type="copy"):
        """拉取某一类后台任务，返回 `{"undone": [...], "done": [...]}`。

        **查不到时必须返回 None，不能返回空结构** —— 调用方要靠"拿到数据"和
        "没拿到数据"区分「确实没有任务」与「查询失败」，后者绝不能当成任务已完成。

        端点：`GET /api/task/<type>/undone` 与 `/done`（server/handles/task.go
        的 SetupTaskRoute），响应 `data` 是 **TaskInfo 数组**，不是字典。
        """
        try:
            undone = self._request("GET", "/api/task/%s/undone" % task_type)
            done = self._request("GET", "/api/task/%s/done" % task_type)
        except Exception as exc:
            self._log("查询 OpenList 任务列表失败：%s" % exc)
            return None
        return {
            "undone": [t for t in (undone or []) if isinstance(t, dict)],
            "done": [t for t in (done or []) if isinstance(t, dict)],
        }

    def get_task(self, tid):
        """获取单个任务详情（拿不到返回 None）。"""
        try:
            data = self._request("POST", "/api/task/copy/info",
                                 query={"tid": str(tid)}, timeout=30)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # -- 任务管理（取消 / 删除 / 重试 / 清除） --
    # OpenList 任务 API（server/handles/task.go 的 taskRoute）：
    #   POST /api/task/<type>/cancel?tid=xxx      取消单个
    #   POST /api/task/<type>/delete?tid=xxx      删除单个
    #   POST /api/task/<type>/retry?tid=xxx       重试单个
    #   POST /api/task/<type>/cancel_some         批量取消（body {"tids": [...]}）
    #   POST /api/task/<type>/delete_some         批量删除（body {"tids": [...]}）
    #   POST /api/task/<type>/retry_failed        重试全部失败项
    #   POST /api/task/<type>/clear_done          清除已完成（成功/失败/取消）
    # 注意：OpenList **没有「暂停」概念**，只有取消。
    #
    # 批量接口的「HTTP 200」不代表成功 —— 见 parse_batch_errors()。历史缺陷就出在
    # 这里：只要请求不抛异常就 `return len(tids)`，于是"一个都没取消"也会报
    # "已取消 N 个"，用户点了「全部取消」，toast 说成功了，任务还在跑。

    def task_action(self, action, tid, task_type="copy", timeout=15):
        """对**单个**任务执行 cancel / delete / retry，返回是否成功。

        单条接口的语义比批量清楚：任务不存在时服务端会返回非 200 的 code，
        `_request` 直接抛错，于是这里能给出确定的 False（而不是"HTTP 通了就算成"）。
        """
        tid = str(tid or "").strip()
        if not tid:
            return False
        label = TASK_ACTION_LABELS.get(action, action)
        try:
            self._request("POST", "/api/task/%s/%s" % (task_type, action),
                          query={"tid": tid}, timeout=timeout)
            return True
        except Exception as exc:
            self._log("%s OpenList 任务失败（%s/%s）：%s" % (label, task_type, tid, exc))
            return False

    def cancel_task(self, tid, task_type="copy"):
        return self.task_action("cancel", tid, task_type)

    def delete_task(self, tid, task_type="copy"):
        return self.task_action("delete", tid, task_type)

    def retry_task(self, tid, task_type="copy"):
        return self.task_action("retry", tid, task_type)

    def batch_task_action(self, action, tids, task_type="copy"):
        """批量执行任务动作，**逐条核实**结果。返回报告 dict：

        ```
        {"action", "taskType", "requested", "ok",
         "failed": [{"id", "error"}], "unknown": [tid, ...]}
        ```

        语义约定（前端文案与测试都依赖它）：

        * `ok` —— 批量接口明确报成功 + 逐条兜底成功的条数；
        * `failed` —— 批量报错、逐条兜底也失败的 tid（原因照抄服务端）；
        * `unknown` —— 批量接口没给逐条结果，且**没有**做逐条兜底，状态未知。
          调用方必须回读任务列表来定论；绝不能把 unknown 算成成功。
        """
        tids = normalize_tids(tids)
        report = {"action": action, "taskType": task_type, "requested": len(tids),
                  "ok": 0, "failed": [], "unknown": []}
        if not tids:
            return report
        label = TASK_ACTION_LABELS.get(action, action)

        errors = None
        try:
            data = self._request("POST", "/api/task/%s/%s_some" % (task_type, action),
                                 json_body={"tids": tids}, timeout=30)
            errors = parse_batch_errors(data)
            if errors is None:
                self._log("OpenList 批量%s接口没有返回逐条结果（%s，%d 项），"
                          "状态未知，交由调用方回读核实" % (label, task_type, len(tids)))
        except Exception as exc:
            self._log("批量%s OpenList 任务失败（%s，%d 项）：%s"
                      % (label, task_type, len(tids), exc))
            errors = dict((tid, str(exc)) for tid in tids)

        if errors is None:
            # 服务端没说哪些成功哪些失败 → 不猜，逐条兜底交给调用方的回读
            report["unknown"] = list(tids)
            return report

        retry_ids = [tid for tid in tids if tid in errors]
        report["ok"] = len(tids) - len(retry_ids)
        for tid in retry_ids:
            if self.task_action(action, tid, task_type):
                report["ok"] += 1
            else:
                report["failed"].append({
                    "id": tid,
                    "error": errors.get(tid) or ("%s失败" % label),
                })
        return report

    def cancel_tasks(self, tids, task_type="copy"):
        """批量取消。返回 batch_task_action 的报告（**不是**成功数）。"""
        return self.batch_task_action("cancel", tids, task_type=task_type)

    def delete_tasks(self, tids, task_type="copy"):
        """批量删除。返回 batch_task_action 的报告。"""
        return self.batch_task_action("delete", tids, task_type=task_type)

    def retry_tasks(self, tids, task_type="copy"):
        """批量重试。返回 batch_task_action 的报告。"""
        return self.batch_task_action("retry", tids, task_type=task_type)

    def clear_done_tasks(self, task_type="copy"):
        """清除某一类已完成的 OpenList 任务（成功/失败/取消）。返回是否成功。"""
        try:
            self._request("POST", "/api/task/%s/clear_done" % task_type, timeout=30)
            return True
        except Exception as exc:
            self._log("清除 OpenList 已完成任务失败（%s）：%s" % (task_type, exc))
            return False

    def retry_failed_tasks(self, task_type="copy"):
        """让服务端重试某一类任务里所有失败项。返回核算报告：

        ```
        {"ok": bool, "taskType", "called": bool,
         "failedBefore": int|None, "failedAfter": int|None,
         "retried": int|None, "error": str}
        ```

        `retry_failed` **不返回重试条数**，只能做「调用前后各数一次 done 里的
        失败项」的差。任一侧拿不到列表就把 `retried` 置 None —— 前端据此如实说
        「已提交，条数无法核实」，绝不编一个数字出来。
        """
        report = {"ok": False, "taskType": task_type, "called": False,
                  "failedBefore": None, "failedAfter": None,
                  "retried": None, "error": ""}

        before = self.list_tasks(task_type)
        if before is not None:
            report["failedBefore"] = count_failed_tasks(before.get("done"))
        try:
            self._request("POST", "/api/task/%s/retry_failed" % task_type, timeout=30)
        except Exception as exc:
            self._log("重试 OpenList 失败任务失败（%s）：%s" % (task_type, exc))
            report["error"] = str(exc)
            report["failedAfter"] = report["failedBefore"]
            report["retried"] = 0 if report["failedBefore"] is not None else None
            return report

        report["ok"] = True
        report["called"] = True
        after = self.list_tasks(task_type)
        if after is not None:
            report["failedAfter"] = count_failed_tasks(after.get("done"))
        if report["failedBefore"] is not None and report["failedAfter"] is not None:
            report["retried"] = max(0, report["failedBefore"] - report["failedAfter"])
        return report

    # -- 下载：云端 → 本地 --

    DOWNLOAD_CHUNK = 256 * 1024

    def download_url(self, path):
        data = self.get(path)
        return (data or {}).get("raw_url", "")

    def absolute_url(self, url):
        """把 `fs/get` 返回的 raw_url 规范成可直接请求的绝对地址。

        OpenList 在不同驱动/配置下会给出两种形态：`/d/xxx`（相对本站，可被
        反代改过前缀）与 `https://cdn.../xxx`（直链）。相对地址必须以
        **OpenList 自身的 base_url** 为前缀——拿请求里的 Host 去拼会在网关
        转发场景下拼到应用自己的地址上，直接 404。
        """
        url = (url or "").strip()
        if not url:
            return ""
        if url.startswith("//"):
            scheme = "https:" if self.base_url().startswith("https") else "http:"
            return scheme + url
        if url.startswith("/"):
            return self.base_url().rstrip("/") + url
        return url

    def _same_origin(self, url):
        """下载地址是否就指向本 OpenList。

        只有同源才带 `Authorization`。raw_url 常常是网盘给的**第三方直链**，
        把本站 token 附到外站请求上既没用、又是一次凭据泄露。
        """
        from urllib.parse import urlsplit
        try:
            a, b = urlsplit(url), urlsplit(self.base_url())
        except Exception:
            return False
        if not a.hostname or not b.hostname:
            return False
        port_a = a.port or (443 if a.scheme == "https" else 80)
        port_b = b.port or (443 if b.scheme == "https" else 80)
        return (a.hostname.lower(), port_a) == (b.hostname.lower(), port_b)

    def _open_download(self, remote_path, part_size, node=None):
        """打开下载流。401 时重登并重新取一次 raw_url（签名地址也会过期）。

        node：调用方已经查过 stat 时直接传进来，省掉一次往返。
        """
        last_error = None
        for attempt in (0, 1):
            if node is None:
                node = self.stat(remote_path)
            if node is None:
                raise OpenlistError("云端找不到：%s" % remote_path)
            if node.get("is_dir"):
                raise OpenlistError("这是一个目录，不能直接下载：%s" % remote_path)
            raw = (node.get("raw_url") or "").strip()
            if not raw:
                raise OpenlistError(
                    "云端没有提供下载地址：%s（该存储驱动可能不支持直接下载）" % remote_path)
            url = self.absolute_url(raw)

            headers = {"Accept": "*/*"}
            if self._same_origin(url):
                token = self.store.token()
                if token:
                    headers["Authorization"] = token
            if part_size:
                headers["Range"] = "bytes=%d-" % part_size

            try:
                resp = httpclient.request("GET", url, headers=headers, timeout=3600)
            except httpclient.TransportError as exc:
                raise NetworkError("下载连接失败：%s" % exc, status=exc.status)

            if resp.status in (401, 403) and self._same_origin(url):
                resp.close()
                last_error = AuthError("下载被拒绝（HTTP %s）" % resp.status)
                if attempt == 0 and self._relogin():
                    node = None     # 重登后签名地址会变，必须重新查一次
                    continue
                raise last_error
            if resp.status >= 400:
                body = resp.read_all()[:200]
                resp.close()
                raise OpenlistError("下载失败（HTTP %s）：%s %s"
                                    % (resp.status, remote_path,
                                       body.decode("utf-8", "replace")),
                                    status=resp.status)
            return resp, node
        raise last_error or OpenlistError("下载失败：%s" % remote_path)

    def download_to_file(self, remote_path, local_path, on_progress=None,
                         should_cancel=None, expected_size=None, timeout=None):
        """把云端单个文件流式下载到本地。支持断点续传与取消。

        三条必须守住的规则：
        1. **写临时文件再改名**。直接写目标路径的话，中断会留下半截文件，
           而它的大小恰好可能"看起来正常"，下一次比对就把它当成已下载。
           只有校验通过才 `os.replace` 到最终路径，所以目标路径要么是旧内容、
           要么是完整新内容。
        2. **续传要看服务端脸色**。带了 `Range` 但服务端回 200（不支持 Range），
           必须清空重写，否则会把整份内容追加到半截文件后面。
        3. **大小必须核对**。`Content-Length`（或比对结果给出的云端大小）与实际
           写入字节不一致就报失败，绝不把残文件当成功。
        """
        part = local_path + ".ddpart"
        meta_path = part + ".meta"
        parent = os.path.dirname(local_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise OpenlistError("创建本地目录失败：%s（%s）" % (parent, exc))

        # 续传前必须先确认「远端还是不是上次那个文件」。
        # 只比对本地临时文件大小远远不够：上次中断后云端同名文件被换成同大小的
        # 新内容时，旧写法会从旧的字节数接着写，最终拼出一个大小正确、内容错误的
        # 文件并报成功（实测：残留 AAAAA + 新内容 BBBBBBBBBB → AAAAA BBBBB，
        # 大小校验全过、os.replace 落地、任务标 done）。
        # 证明不了是同一个文件就不续传 —— 宁可重下，也不产出坏文件。
        node = self.stat(remote_path)
        if node is None:
            raise OpenlistError("云端找不到：%s" % remote_path)
        if node.get("is_dir"):
            raise OpenlistError("这是一个目录，不能直接下载：%s" % remote_path)

        done = 0
        if os.path.isfile(part):
            try:
                done = int(os.path.getsize(part))
            except OSError:
                done = 0
            if done and not self._part_meta_matches(meta_path, node, remote_path):
                self._log("续传作废（无法确认云端文件仍是上次那个）：%s" % remote_path)
                done = 0

        want = int(expected_size or 0)
        if done and want and done >= want:
            # 临时文件已经不比目标小：续传没有意义，重来一次更安全
            done = 0
        if not done:
            self._drop_part(part, meta_path)

        resp, node = self._open_download(remote_path, done, node=node)
        # 真正开始写之前记下本次远端指纹：下次续传据此判断还能不能接着写。
        self._write_part_meta(meta_path, remote_path, node)
        cloud_size = int(node.get("size") or 0)
        if not want:
            want = cloud_size

        try:
            declared = resp.header("content-length", "")
            declared = int(declared) if str(declared).isdigit() else None

            if resp.status == 206 and done:
                mode = "ab"
                written = done
            else:
                # 服务端忽略了 Range（回 200 全量），必须从头写
                mode = "wb"
                written = 0
                done = 0

            cancel = should_cancel or (lambda: False)
            with open(part, mode) as fh:
                for chunk in resp.iter_chunks(self.DOWNLOAD_CHUNK):
                    if cancel():
                        raise UploadCancelled("已取消")
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, want or written)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        finally:
            resp.close()

        if want and written != want:
            raise OpenlistError("下载不完整（%s：%d / %d 字节）"
                                % (remote_path, written, want))
        if (not want) and declared is not None and written != declared:
            raise OpenlistError("下载不完整（%s：%d / %d 字节）"
                                % (remote_path, written, declared))

        try:
            os.replace(part, local_path)
        except OSError as exc:
            raise OpenlistError("写入本地文件失败：%s（%s）" % (local_path, exc))
        self._drop_part(None, meta_path)     # 落地成功，指纹没必要留着
        return {"size": written, "path": local_path}

    # -- 断点续传指纹 --

    @staticmethod
    def _part_meta_matches(meta_path, node, remote_path=None):
        """本地残留的半成品是否确实属于远端当前这个文件。

        判据：
        1. **必须有一份本次下载写下的指纹**（`*.ddpart.meta`）。老版本或别处
           留下的裸半成品一律不续传 —— 谁也不知道它是不是同一个文件的头半截。
        2. 指纹里的远程路径要对得上（同名不同目录的残留不能互相续传）。
        3. 大小相等。
        4. 双方都提供 modified 时必须相等；缺 modified（部分驱动不给）时只能
           按大小判 —— 这条是历史能力的下限，不是新增风险：既然拿不到任何
           版本标识，本来就没法区分"同尺寸新内容"，此时至少保证不是陌生残留。
        """
        import json as _json
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = _json.load(fh)
        except Exception:
            return False
        try:
            if int(meta.get("size") or -1) != int(node.get("size") or 0):
                return False
            if remote_path is not None and str(meta.get("path") or "") != remote_path:
                return False
            old_modified = str(meta.get("modified") or "")
            now_modified = str(node.get("modified") or "")
            if old_modified and now_modified:
                return old_modified == now_modified
            return True
        except Exception:
            return False

    @staticmethod
    def _write_part_meta(meta_path, remote_path, node):
        import json as _json
        try:
            with open(meta_path, "w", encoding="utf-8") as fh:
                _json.dump({"path": remote_path,
                            "size": int(node.get("size") or 0),
                            "modified": str(node.get("modified") or "")}, fh)
        except OSError:
            pass

    @staticmethod
    def _drop_part(part, meta_path):
        for p in (part, meta_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except OSError:
                pass

    # -- 删除 --

    def remove_many(self, paths):
        """批量删除云端路径（文件或目录；目录在 OpenList 侧是递归删除）。

        按父目录分组，一次请求删同一个目录下的多个名字 —— 批量清理动辄几百项，
        逐条发请求是几十秒的差别。整组失败时**降级为逐条删**：一条坏条目
        （比如已被别处删掉）不该拖垮同组其它条目，而且逐条才能给出精确失败原因。

        返回 `(removed_paths, failures)`，failures 是 `[{"path", "error"}]`。
        """
        groups = {}
        failures = []
        for raw in paths or []:
            # rstrip("/") 之后 `/`、`//`、`\` 都会退化成空串 —— 一次 `not p`
            # 就同时盖住了「空输入」和「云端根目录」，不必再单独判 `p == "/"`
            # （那条分支永远不可达，只会让人误以为它管着什么）。
            p = str(raw or "").strip().replace("\\", "/").rstrip("/")
            if not p:
                failures.append({"path": str(raw), "error": "不允许删除云端根目录"})
                continue
            parent, _, name = p.rpartition("/")
            groups.setdefault(parent or "/", []).append((name, p))

        removed = []
        for parent in sorted(groups):
            pairs = groups[parent]
            names = [n for n, _ in pairs]
            try:
                self.remove(parent, names)
                removed.extend(p for _, p in pairs)
                continue
            except OpenlistError as exc:
                self._log("批量删除失败（%s，%d 项），改为逐条：%s"
                          % (parent, len(names), exc))
            for name, full in pairs:
                try:
                    self.remove(parent, [name])
                    removed.append(full)
                except OpenlistError as exc:
                    failures.append({"path": full, "error": str(exc)})
        return removed, failures

    # -- 上传：小文件同步直传 --

    def upload_file(self, local_path, remote_path, on_progress=None, overwrite=True,
                    last_modified=None, verify=True, timeout=None):
        """同步直传一个文件（流式，内存占用恒定）。完成后可选校验大小。

        remote_path 是**完整目标路径**（含文件名）。
        """
        size = os.path.getsize(local_path)
        base = self.base_url()
        url = base + "/api/fs/put"
        headers = {
            "File-Path": quote(remote_path, safe=""),
            "Content-Type": "application/octet-stream",
            "Overwrite": "true" if overwrite else "false",
        }
        if last_modified:
            headers["Last-Modified"] = str(int(last_modified * 1000))
        timeout = timeout or max(600, int(size / 100000) + 300)   # 按大小放宽

        return self._put_with_auth(url, headers, local_path, size, on_progress,
                                   timeout, remote_path, verify)

    def _put_with_auth(self, url, headers, local_path, size, on_progress, timeout,
                       remote_path, verify):
        """带 token 与 401 重登的流式 PUT。body 从磁盘流式读，不进内存。"""
        def _do():
            token_headers = dict(headers)
            token_headers["Authorization"] = self.store.token()
            with open(local_path, "rb") as fh:
                resp = httpclient.put_stream(url, token_headers, fh, size,
                                             on_progress=on_progress, timeout=timeout)
                try:
                    raw = resp.read_all()
                finally:
                    resp.close()
            return resp.status, raw

        status, raw = self._auth_retry(_do)
        self._check_upload_response(status, raw, remote_path)
        if verify:
            self.verify_uploaded(remote_path, size)
        return {"size": size, "path": remote_path}

    def _auth_retry(self, do_request):
        """执行一次「返回 (status, raw)」的请求；401 时重登再来一次。"""
        status, raw = do_request()
        if status == 401:
            if not self._relogin():
                raise AuthError("登录态失效，请重新登录")
            status, raw = do_request()
        return status, raw

    def _check_upload_response(self, status, raw, remote_path):
        import json as _json
        # 某些存储驱动在直传成功后返回空 body 或非 JSON（如 204），
        # 只要 HTTP 层成功就视为上传受理成功，后续 verify_uploaded 兜底。
        if 200 <= status < 300 and (not raw or raw.strip() == b""):
            return
        try:
            payload = _json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if status == 200 and code == 200:
            return
        # 不是标准 JSON 壳但 HTTP 2xx：同样视为受理成功
        if 200 <= status < 300 and code is None:
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        if not message:
            message = raw[:200].decode("utf-8", "replace") if raw else ("HTTP %s" % status)
        if status == 401 or code == 401:
            raise AuthError(str(message))
        raise OpenlistError("上传失败（%s）：%s" % (remote_path, message),
                            code=code, status=status, kind="business")

    def verify_uploaded(self, remote_path, expected_size):
        """上传完成后读回云端条目，比对大小。不一致抛错（上层据此判失败重传）。"""
        node = self.stat(remote_path)
        if node is None:
            raise OpenlistError("上传后校验失败：云端找不到 %s" % remote_path)
        cloud_size = int(node.get("size") or 0)
        if cloud_size != int(expected_size):
            raise OpenlistError(
                "上传后校验失败：大小不一致（本地 %d / 云端 %d）：%s"
                % (expected_size, cloud_size, remote_path))
        return True

    # -- 上传：大文件分片（断点续传） --

    def mp_init(self, remote_path, size, chunk_size=None, overwrite=True,
                last_modified=None):
        """初始化/恢复分片会话。返回规范化的 snapshot。

        服务端没开 multipart 时返回 403「multipart upload is disabled」，
        由调用方降级到同步直传。
        """
        base = self.base_url()
        headers = {
            "File-Path": quote(remote_path, safe=""),
            "X-File-Size": str(int(size)),
            "Overwrite": "true" if overwrite else "false",
            "Content-Type": "application/octet-stream",
        }
        if chunk_size:
            headers["X-Chunk-Size"] = str(int(chunk_size))
        if last_modified:
            headers["Last-Modified"] = str(int(last_modified * 1000))
        data = self._request("POST", "/api/fs/multipart/init", headers=headers,
                             timeout=60)
        return self._normalize_snapshot(data or {})

    def mp_send_chunk(self, upload_id, index, data, timeout=None):
        """上传一个分片（幂等，可重传）。data 是 bytes。"""
        base = self.base_url()
        url = base + "/api/fs/multipart/chunk"
        headers = {
            "X-Upload-Id": upload_id,
            "X-Chunk-Index": str(int(index)),
            "Content-Type": "application/octet-stream",
        }
        timeout = timeout or 300

        def _do():
            token_headers = dict(headers)
            token_headers["Authorization"] = self.store.token()
            resp = httpclient.put_stream(url, token_headers, data, len(data),
                                         timeout=timeout)
            try:
                raw = resp.read_all()
            finally:
                resp.close()
            return resp.status, raw

        status, raw = self._auth_retry(_do)
        self._check_upload_response(status, raw, "分片 %d" % index)
        return True

    def mp_complete(self, upload_id, timeout=None):
        base = self.base_url()
        data = self._request("POST", "/api/fs/multipart/complete",
                             headers={"X-Upload-Id": upload_id},
                             timeout=timeout or 300)
        return self._normalize_snapshot(data or {})

    def mp_status(self, upload_id=None, path=None, size=None):
        query = {}
        if upload_id:
            query["upload_id"] = upload_id
        elif path and size is not None:
            query["path"] = path
            query["size"] = str(int(size))
        else:
            raise OpenlistError("mp_status 需要 upload_id 或 (path, size)")
        data = self._request("GET", "/api/fs/multipart/status", query=query, timeout=60)
        return self._normalize_snapshot(data or {})

    def mp_abort(self, upload_id):
        try:
            return self._request("POST", "/api/fs/multipart/abort",
                                 headers={"X-Upload-Id": upload_id}, timeout=60)
        except OpenlistError:
            return None     # 中止失败不影响主流程

    @staticmethod
    def _normalize_snapshot(data):
        """把 SessionSnapshot 规整成统一结构，并解析出『已传分片序号集合』。

        received 是**闭区间**列表 [[start,end],...]，表示客户端不必重传的分片。
        """
        received = data.get("received") or []
        done = set()
        for pair in received:
            try:
                start, end = int(pair[0]), int(pair[1])
            except Exception:
                continue
            for i in range(start, end + 1):     # 闭区间，含 end
                done.add(i)
        return {
            "upload_id": data.get("upload_id", ""),
            "state": data.get("state", 0),
            "path": data.get("path", ""),
            "size": int(data.get("size") or 0),
            "chunk_size": int(data.get("chunk_size") or 0),
            "total_chunks": int(data.get("total_chunks") or 0),
            "received": received,
            "done_chunks": done,
            "received_bytes": int(data.get("received_bytes") or 0),
            "frontier": int(data.get("frontier") or 0),
            "storage_progress": float(data.get("storage_progress") or 0),
            "resumed": bool(data.get("resumed")),
            "error": data.get("error", ""),
        }

    def upload_resumable(self, local_path, remote_path, on_progress=None,
                         chunk_size=DEFAULT_CHUNK_SIZE, overwrite=True,
                         last_modified=None, verify=True, should_cancel=None,
                         session=None):
        """智能上传：优先 multipart 分片（断点续传），不行降级同步直传。

        session: 可选的已恢复会话（dict，含 upload_id/done_chunks），用于应用重启
                 后继续。没有则重新 init（服务端会按 path+size 自动找回）。
        should_cancel(): 可选回调，返回 True 表示用户取消了（抛 UploadCancelled）。
        on_progress(sent_bytes, total_bytes): 进度回调。
        """
        size = os.path.getsize(local_path)
        if should_cancel and should_cancel():
            raise UploadCancelled("已取消")

        # 小文件或会话为空先尝试分片；init 报 disabled 再降级
        use_multipart = size > SMALL_FILE_THRESHOLD
        if use_multipart:
            try:
                return self._upload_multipart(local_path, remote_path, size, on_progress,
                                              chunk_size, overwrite, last_modified,
                                              verify, should_cancel, session)
            except OpenlistError as exc:
                # 服务端没开 multipart → 降级同步直传
                if "disabled" in str(exc).lower() or exc.status == 403 and "multipart" in str(exc).lower():
                    self._log("服务端未开启分片上传，降级为同步直传：%s" % remote_path)
                else:
                    raise
        return self.upload_file(local_path, remote_path, on_progress=on_progress,
                                overwrite=overwrite, last_modified=last_modified,
                                verify=verify)

    def _upload_multipart(self, local_path, remote_path, size, on_progress,
                          chunk_size, overwrite, last_modified, verify,
                          should_cancel, session):
        if session and session.get("upload_id"):
            snap = session
        else:
            snap = self.mp_init(remote_path, size, chunk_size=chunk_size,
                                overwrite=overwrite, last_modified=last_modified)
        upload_id = snap["upload_id"]
        if not upload_id:
            raise OpenlistError("分片初始化失败：没有拿到 upload_id")
        cs = snap["chunk_size"] or chunk_size
        total_chunks = snap["total_chunks"] or max(1, (size + cs - 1) // cs)
        done = set(snap.get("done_chunks") or set())

        # 已传字节 = 已完成分片的字节和（用于断点续传时的起始进度）
        sent_bytes = 0
        with open(local_path, "rb") as fh:
            for index in range(total_chunks):
                if should_cancel and should_cancel():
                    raise UploadCancelled("已取消")
                offset = index * cs
                length = min(cs, size - offset)
                if index in done:
                    sent_bytes += length
                    if on_progress:
                        on_progress(sent_bytes, size)
                    continue
                fh.seek(offset)
                data = fh.read(length)
                self.mp_send_chunk(upload_id, index, data)
                done.add(index)
                sent_bytes += length
                if on_progress:
                    on_progress(sent_bytes, size)

        self.mp_complete(upload_id)
        if verify:
            self.verify_uploaded(remote_path, size)
        return {"size": size, "path": remote_path, "upload_id": upload_id}

    # -- 体检 --

    def diagnose(self):
        steps = []

        def step(name, fn):
            try:
                detail = fn()
                steps.append({"name": name, "ok": True, "detail": detail})
                return True
            except OpenlistError as exc:
                steps.append({"name": name, "ok": False, "detail": str(exc)})
                return False
            except Exception as exc:
                steps.append({"name": name, "ok": False, "detail": "异常：%s" % exc})
                return False

        step("读取 OpenList 版本", lambda: self._public_version())
        if not step("获取当前用户", lambda: (self.me() or {}).get("username", "?")):
            return {"ok": False, "steps": steps}
        step("列根目录", lambda: "%d 项" % self.list_dir("/", page=1, per_page=1)["total"])
        backend, note = httpclient.detect_backend()
        steps.append({"name": "网络传输", "ok": True, "detail": "%s（%s）" % (backend, note)})
        steps.append({"name": "SHA-256 后端", "ok": True, "detail": hasher.backend()})
        ok = all(s["ok"] for s in steps)
        return {"ok": ok, "steps": steps}

    def _public_version(self):
        import json as _json
        base = self.base_url()
        try:
            resp = httpclient.request("GET", base + "/api/public/settings", timeout=15)
            raw = resp.read_all()
            resp.close()
            data = _json.loads(raw.decode("utf-8", "replace"))
            return (data.get("data") or {}).get("version", "未知")
        except Exception as exc:
            raise NetworkError("取不到版本：%s" % exc)


class UploadCancelled(OpenlistError):
    def __init__(self, message="已取消"):
        OpenlistError.__init__(self, message, kind="business")


def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            if unit == "B":
                return "%d %s" % (int(num), unit)
            return "%.1f %s" % (num, unit)
        num /= 1024.0
