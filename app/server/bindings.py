# -*- coding: utf-8 -*-
"""目录绑定与 OpenList 增量文件复制。

一个绑定（Binding）描述「OpenList 源目录 → 一个或多个云端目录」的备份关系：
* 扫描时对每个启用的云端目标递归列目录，按相对路径对齐源文件，
  标记 missing（云端缺失）/ mismatch（大小不一致）/ uploaded（已一致）。
* 缺失文件可手动逐条/批量上传，也可开启 autoUpload 后自动入队。
* 「上传」= 调 OpenList 的 `fs/copy` 做**服务端复制**（源和目标都是 OpenList 路径）。
  复制结果只认两种证据：OpenList 后台任务的成功状态，或回云端核对文件大小。

**状态判定的铁律**：任何「查不到」都不等于成功。任务列表查询失败、任务已被清理、
返回体里没有任务 id，一律不能推断出「已同步」—— 否则界面会在文件根本没传上去时
显示备份完成。没有确凿证据时保持原状态，等下一次扫描再看。

绑定配置与最近一次扫描结果持久化到 var/bindings.json。
"""

import os
import threading
import time
import uuid

import config
import openlist
import verify as verify_mod

# 「无限深度」时用一个足够大的值，绕过 verify 模块 depth>=0 的限制
UNLIMITED_DEPTH = 999

# 绑定文件在各云端目标上的状态
ST_MISSING = "missing"       # 云端缺失
ST_MISMATCH = "mismatch"     # 云端有但大小不一致
ST_UPLOADED = "uploaded"     # 云端已存在且大小一致
ST_QUEUED = "queued"         # 已提交复制，等待服务端完成
ST_UPLOADING = "uploading"   # 服务端正在复制
ST_FAILED = "failed"         # 复制失败
ST_DONE = "done"             # 兼容旧数据：等同 uploaded

# 同步模式。历史上还提供过 full（镜像删除）/ move（移动后删源），
# 两者都没实现，而且与应用「只做单向上传、不动云端与本地已有数据」的承诺冲突，
# 已下线 —— 老数据里若残留，读取时统一降级为 add。
SYNC_ADD = "add"           # 只新增：复制源有目标没有的
SYNC_MODES = (SYNC_ADD,)

# 冲突策略
CONFLICT_SKIP = "skip"     # 跳过已存在（服务端 skip_existing）
CONFLICT_OVERWRITE = "overwrite"  # 覆盖（服务端 overwrite）
CONFLICT_ABORT = "abort"   # 中止报错
CONFLICT_POLICIES = (CONFLICT_SKIP, CONFLICT_OVERWRITE, CONFLICT_ABORT)

# 各冲突策略对应的 fs/copy 开关（见 openlist.OpenlistClient.copy 的说明）
CONFLICT_FLAGS = {
    CONFLICT_SKIP: {"skip_existing": True},
    CONFLICT_OVERWRITE: {"overwrite": True},
    CONFLICT_ABORT: {},
}

# 仍需处理（可上传）的状态
PENDING_STATES = (ST_MISSING, ST_MISMATCH, ST_FAILED)
# 提交后在服务端产生、尚未定论的状态
IN_FLIGHT_STATES = (ST_QUEUED, ST_UPLOADING)
# 已确认同步完成的状态
SETTLED_STATES = (ST_UPLOADED, ST_DONE)

# 「提交上传」这个动作本身也要能被观察。
#
# 点一次「一键上传」不是一瞬间的事：选中的文件夹里有多少个子目录，就要发多少次
# `ensure_dir` + `fs/copy`（每个子目录一组）。几十个子目录就是上百次网络往返，
# 十几秒起步。这段时间如果前端什么都不显示，用户只会认为"点了没反应"，
# 然后反复点击 —— 每一次点击都是一份重复的复制请求。
#
# 这些阶段名由后端产生、前端照原样展示，双方不用各自猜。
SUBMIT_PREPARE = "prepare"      # 正在收集待提交的文件
SUBMIT_SENDING = "submit"       # 正在把复制请求逐组提交给 OpenList
SUBMIT_FINALIZE = "finalize"    # 正在写回状态、记录历史、落盘
SUBMIT_DONE = "done"            # 提交结束（成功或部分失败，看 error 字段）
SUBMIT_FAILED = "error"         # 提交异常中断

# 提交后多久还没有任何结果（既无任务 id、云端也看不到文件）就判定失败。
# 同存储内的 fs/copy 是同步完成的，正常情况下提交那一刻就已经落盘，
# 所以这个阈值只需要覆盖"跨存储但服务端没给出任务 id"的少数情况。
QUEUED_STALE_SECONDS = 1800

# 已完成的绑定任务在「传输列表」里保留多久（秒）。
#
# 绑定文件复制完成后状态变成 uploaded（云端已有），于是从"进行中"集合里退出 ——
# 如果就此把它从传输列表里摘掉，用户看到的就是「传完那一刻任务凭空消失」，
# 而本地上传队列的终态任务是**保留**在列表里的，两边表现必须一致。
#
# 这里用时间窗口而不是给绑定文件加持久标记：绑定文件的状态是扫描结果的一部分，
# 不能为了列表好看去动它（删了会导致下次扫描重复判缺失、重复上传）。
# 另外重新扫描会把已同步的文件移出 files 列表，列表自然归零，不需要额外清理。
BINDING_DONE_VISIBLE_SECONDS = 600
# 同一窗口内最多显示多少条已完成：一次上传几千个文件时，列表还得能用。
BINDING_DONE_MAX = 500

# 扫描状态
SCAN_IDLE = "idle"
SCAN_RUNNING = "scanning"
SCAN_DONE = "done"
SCAN_ERROR = "error"

DEFAULT_BINDING_DEPTH = UNLIMITED_DEPTH
DEFAULT_EXCLUDES = list(config.DEFAULT_CONFIG.get("excludeNames", []))

# 快照格式版本。快照只在 sourceSnapshot 打开时写入；格式对不上就直接丢弃重扫，
# 避免拿旧结构去比对得出"全都没变"的假结论。
# v3：每个文件额外记录各目标的判定结果，未变化且已同步的文件可以完全跳过网络请求。
SNAPSHOT_VERSION = 3


def join_cloud(base, rel):
    """把云端目录与相对路径拼成规范化的云端绝对路径。"""
    parts = [p for p in (str(base or "/") + "/" + str(rel or "")).replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts)


def normalize_cloud(path):
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts)


def _copy_json(value):
    """给要落盘的 JSON 结构做一次深拷贝（只处理 dict/list/标量）。"""
    if isinstance(value, dict):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(v) for v in value]
    return value


def _new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex[:10]


def _now():
    return int(time.time())


# 「目标端查不出来」的哨兵值。**绝不能**和 None（云端确实没有这个文件）混为一谈：
# 二者会导出完全相反的结论 —— None → missing（待上传），_UNKNOWN → 无法判定（沿用上次结论）。
_UNKNOWN = object()


class CloudEntryLookup(object):
    """按**父目录**批量查询云端条目（替代「每个文件一次 stat」）。

    为什么需要：快照模式下每个新增/变化的文件、每个目标都要发一次 `fs/get`
    （即 stat）。一次新增 5000 个文件、2 个目标 = 1 万次串行请求 —— 这是绑定
    扫描最慢的一段，也是「增量扫描」本该避免却没避免的。改成按父目录分组后，
    同一个目录下的 N 个文件只发一次 `fs/list`，请求数从「文件数」降到「目录数」。

    语义与逐文件 stat **完全等价**：
    * 目录列得出、里面没这个名字 → `None`（云端确实没有 → missing）
    * 目录**列不出来**（网络/权限） → `_UNKNOWN`（无法判定，不得当成"云端没有"）
    """

    def __init__(self, client, base_path, concurrency=None, log=None):
        self.client = client
        self.base = base_path or "/"
        self.workers = verify_mod.normalize_concurrency(concurrency)
        self._log = log or (lambda *a, **k: None)
        self._cache = {}          # parent_rel -> {name: node}；None 表示该目录列不出来
        self.listed = 0           # 真正发出的列表请求数（测试用）

    @staticmethod
    def split(rel):
        rel = str(rel or "")
        if "/" in rel:
            parent, _, name = rel.rpartition("/")
            return parent, name
        return "", rel

    def prefetch(self, rels):
        """把这批文件涉及的父目录**并发**列一遍，之后 get() 直接命中缓存。"""
        parents = []
        seen = set()
        for rel in rels or ():
            parent, _ = self.split(rel)
            if parent not in seen:
                seen.add(parent)
                parents.append(parent)
        todo = [p for p in parents if p not in self._cache]
        if not todo:
            return
        for parent, result in zip(todo, verify_mod.map_concurrent(
                self._list_parent, todo, self.workers)):
            # map_concurrent 会把未捕获的异常原样放进结果里，这里统一收敛成 None
            self._cache[parent] = None if isinstance(result, BaseException) else result

    def get(self, rel):
        parent, name = self.split(rel)
        if parent not in self._cache:
            self._cache[parent] = self._list_parent(parent)
        children = self._cache[parent]
        if children is None:
            return _UNKNOWN
        return children.get(name)

    def _list_parent(self, parent):
        abs_dir = join_cloud(self.base, parent)
        self.listed += 1
        try:
            nodes = self.client.list_all(abs_dir)
        except Exception as exc:
            self._log("列云端目录失败（%s）：%s" % (abs_dir, exc))
            return None
        out = {}
        for node in nodes or ():
            if not isinstance(node, dict):
                continue
            name = node.get("name")
            if name:
                out[str(name)] = node
        return out


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

class BindingStore(object):
    """绑定关系的加载、持久化与业务操作。"""

    def __init__(self, client, path=None, queue=None, log=None):
        self.client = client
        self.queue = queue
        self.path = path or os.path.join(config.data_dir(), "bindings.json")
        self._log = log or (lambda *a, **k: None)
        self._lock = threading.RLock()
        # 提交上传的**瞬态**进度：binding_id -> {...}。刻意不落盘 ——
        # 它是某个线程正在做的事情的镜像，重启后本来就不存在，
        # 写进 bindings.json 只会多一份会和内存打架的陈旧数据。
        self._submit = {}
        # 单独一把锁：这上面的更新发生在每个子目录提交完成时（高频、来自并发
        # worker），不该和扫描/落盘去抢同一把 _lock。
        self._submit_lock = threading.RLock()
        self._bindings = []
        self._load_failed = False    # 解析失败时置位，拒绝自动 save 空列表
        self._scan_threads = {}
        self._scan_flags = {}
        self._auto_timer = None
        self._auto_stop = threading.Event()
        # 全局扫描并发限制：同时最多 N 个绑定在扫描，避免多个绑定同时打同一个云盘
        self._scan_concurrency = 2
        self._scan_semaphore = threading.Semaphore(self._scan_concurrency)
        self.load()

    # -- 持久化 --

    def load(self):
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("bindings") if isinstance(data, dict) else None
            if isinstance(items, list):
                self._bindings = [b for b in items if isinstance(b, dict)]
        except Exception as exc:
            # 解析失败不能静默清零：下一次 save() 会把空列表写回，原文件彻底消失。
            # 把坏文件改名备份 + 记日志，让用户有机会恢复。
            self._log("绑定配置解析失败，已备份原文件：%s" % exc)
            try:
                import time
                bak = self.path + ".corrupt-%d" % int(time.time())
                if os.path.exists(self.path):
                    os.rename(self.path, bak)
                    self._log("坏文件已备份为：%s" % bak)
            except Exception:
                pass
            self._bindings = []
            self._load_failed = True
        # 启动时把扫描中状态重置为 idle（上次崩溃残留）
        for b in self._bindings:
            if b.get("scanStatus") == SCAN_RUNNING:
                b["scanStatus"] = SCAN_IDLE
                b["scanError"] = ""

    def save(self):
        # 加载失败且当前没有绑定 → 拒绝自动 save 空列表，避免覆盖原文件。
        # 用户手动新增绑定后 _bindings 非空，此时可以正常保存。
        if self._load_failed and not self._bindings:
            self._log("绑定配置曾解析失败，当前为空，拒绝自动落盘以保护原文件")
            return
        with self._lock:
            # 在锁内做深拷贝快照：json.dump 在锁外跑，若直接把 self._bindings
            # 的引用交出去，并发修改会让序列化抛
            # "dictionary changed size during iteration"，落盘静默失败。
            payload = {"version": 1, "savedAt": _now(),
                       "bindings": [_copy_json(b) for b in self._bindings]}
        try:
            config.atomic_write_json(self.path, payload)
            self._load_failed = False    # 成功保存后清除失败标志
        except Exception as exc:
            self._log("绑定关系落盘失败：%s" % exc)

    # -- 提交上传的进度与幂等 --

    @staticmethod
    def _submit_key(binding_id):
        """把 binding id 规整成可作字典键的字符串。

        HTTP 的查询参数经 `parse_qs` 解析后，值**都是 list**；哪条路径忘了取
        首个元素就直接传进来，字典查找会抛 `unhashable type: 'list'`，
        整个接口 500 —— 对用户就是「点了没反应」。这里兜住：最差也只是
        「查不到」，不会把接口打崩。
        """
        if isinstance(binding_id, (list, tuple)):
            binding_id = binding_id[0] if binding_id else ""
        return binding_id if isinstance(binding_id, str) else str(binding_id or "")

    def _submit_begin(self, binding_id):
        """登记一次提交的开始（此刻还不知道总量，分组完成后再补）。"""
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            self._submit[binding_id] = {
                "active": True,
                "phase": SUBMIT_PREPARE,
                "done": 0, "total": 0,
                "files": 0, "targets": 0,
                "submitted": 0, "failed": 0,
                "message": "", "startedAt": _now(), "finishedAt": 0,
            }

    def _submit_try_begin(self, binding_id):
        """原子地「检查是否正在提交 + 登记开始」，返回是否成功。

        必须在同一把锁里完成检查和登记，否则两个请求都能穿过检查
        （TOCTOU），后者覆盖前者的进度字典，两批 fs/copy 都真的发出去。
        """
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            cur = self._submit.get(binding_id)
            if cur and cur.get("active"):
                return False
            self._submit[binding_id] = {
                "active": True,
                "phase": SUBMIT_PREPARE,
                "done": 0, "total": 0,
                "files": 0, "targets": 0,
                "submitted": 0, "failed": 0,
                "message": "", "startedAt": _now(), "finishedAt": 0,
            }
            return True

    def _submit_update(self, binding_id, **fields):
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            cur = self._submit.get(binding_id)
            if cur is not None:
                cur.update(fields)

    def _submit_bump(self, binding_id):
        """一个目录组提交结束（无论成败）→ 进度前进一格。

        失败的那组同样要前进：否则一次失败就让进度条永远卡在半路，
        用户又回到「不知道还在不在跑」的状态 —— 那正是要消灭的体验。
        """
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            cur = self._submit.get(binding_id)
            if cur is not None:
                cur["done"] = int(cur.get("done") or 0) + 1

    def _submit_end(self, binding_id, phase=SUBMIT_DONE, message=""):
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            cur = self._submit.get(binding_id)
            if cur is None:
                return
            cur["active"] = False
            cur["phase"] = phase
            # 收尾时把 done 对齐到 total：最后几组的结果已经汇总完了，
            # 不该让进度条停在 97% 这种数字上。
            cur["done"] = max(int(cur.get("done") or 0), int(cur.get("total") or 0))
            if message:
                cur["message"] = message
            cur["finishedAt"] = _now()

    def _submit_view(self, binding_id):
        """给前端看的提交进度快照：字段恒定齐全，调用方不必做空值判断。"""
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            cur = self._submit.get(binding_id) or {}
            view = {
                "active": bool(cur.get("active")),
                "phase": cur.get("phase") or "",
                "done": int(cur.get("done") or 0),
                "total": int(cur.get("total") or 0),
                "files": int(cur.get("files") or 0),
                "targets": int(cur.get("targets") or 0),
                "submitted": int(cur.get("submitted") or 0),
                "failed": int(cur.get("failed") or 0),
                "message": cur.get("message") or "",
                "startedAt": int(cur.get("startedAt") or 0),
                "finishedAt": int(cur.get("finishedAt") or 0),
            }
        # 百分比在后端算好：前端只负责画。它在提交阶段的分母是「目录组数」而不是
        # 文件数，这个口径不该让前端去猜。
        if view["total"] > 0:
            view["percent"] = min(100, int(view["done"] * 100 / view["total"]))
        else:
            view["percent"] = 100 if not view["active"] else 0
        if view["finishedAt"] and view["startedAt"]:
            view["elapsed"] = max(0, view["finishedAt"] - view["startedAt"])
        else:
            view["elapsed"] = 0
        return view

    def is_submitting(self, binding_id):
        """该绑定是否有一次提交正在进行（前端的按钮禁用只是体验层，
        真正的幂等闸门在 `upload_files` 开头）。"""
        binding_id = self._submit_key(binding_id)
        with self._submit_lock:
            return bool((self._submit.get(binding_id) or {}).get("active"))

    # -- 查询 --

    def list_bindings(self):
        """返回绑定列表（含摘要统计）。"""
        with self._lock:
            out = [self._summary(b) for b in self._bindings]
            # 逐个补「是否正在提交上传」。它只存在于内存（`_submit`）、不属于
            # 绑定数据本身，所以 `_summary` 这个纯函数算不出来。前端刷新或重进
            # 页面后靠它恢复「按钮禁用 + 进度条」—— 否则刷新一下按钮就又能点了，
            # 防重复点击形同虚设。
            for item in out:
                item["submitting"] = self.is_submitting(item.get("id"))
            return out

    def get_binding(self, binding_id):
        with self._lock:
            b = self._find(binding_id)
            return dict(b) if b else None

    @staticmethod
    def _summary(b):
        """列表视图：不含完整 files，只给计数。"""
        files = b.get("files") or []
        missing = 0
        in_flight = 0
        for f in files:
            statuses = (f.get("targets") or {}).values()
            if any(s.get("status") in PENDING_STATES for s in statuses):
                missing += 1
            if any(s.get("status") in IN_FLIGHT_STATES for s in statuses):
                in_flight += 1
        return {
            "id": b.get("id"),
            "name": b.get("name"),
            "localPath": b.get("localPath"),
            "cloudTargets": [dict(t) for t in b.get("cloudTargets", [])],
            "scanDepth": b.get("scanDepth", DEFAULT_BINDING_DEPTH),
            "autoUpload": b.get("autoUpload", False),
            "excludeNames": list(b.get("excludeNames", [])),
            "createdAt": b.get("createdAt", 0),
            "updatedAt": b.get("updatedAt", 0),
            "lastScannedAt": b.get("lastScannedAt", 0),
            "scanStatus": b.get("scanStatus", SCAN_IDLE),
            "scanError": b.get("scanError", ""),
            "scanProgress": b.get("scanProgress") or {"phase": "idle", "scanned": 0, "current": ""},
            "scanStats": b.get("scanStats"),
            # 源目录里的文件总数（不是"待处理数"，待处理数在 missingCount）
            "totalLocalFiles": b.get("totalLocalFiles", 0),
            "missingCount": missing,
            "inFlightCount": in_flight,
            "syncMode": b.get("syncMode") if b.get("syncMode") in SYNC_MODES else SYNC_ADD,
            "conflictPolicy": b.get("conflictPolicy") if b.get("conflictPolicy") in CONFLICT_POLICIES
                              else CONFLICT_SKIP,
            "sourceSnapshot": bool(b.get("sourceSnapshot", False)),
            "taskHistory": (b.get("taskHistory") or [])[-20:],
        }

    # -- CRUD --

    def create_binding(self, data):
        raw_local = (data.get("localPath") or "").strip()
        if not raw_local:
            raise ValueError("OpenList 源目录不能为空")
        local_path = normalize_cloud(raw_local)

        targets = self._normalize_targets(data.get("cloudTargets") or [])
        if not targets:
            raise ValueError("至少需要一个云端目录")

        binding = {
            "id": _new_id("bd"),
            "name": (data.get("name") or "").strip() or (local_path.rstrip("/").split("/")[-1] or "/"),
            "localPath": local_path,
            "cloudTargets": targets,
            "scanDepth": self._normalize_depth(data.get("scanDepth")),
            "autoUpload": bool(data.get("autoUpload")),
            "excludeNames": self._normalize_excludes(data.get("excludeNames")),
            "createdAt": _now(),
            "updatedAt": _now(),
            "lastScannedAt": 0,
            "scanStatus": SCAN_IDLE,
            "scanError": "",
            "files": [],
            "totalLocalFiles": 0,
            "syncMode": self._normalize_sync_mode(data.get("syncMode")),
            "conflictPolicy": data.get("conflictPolicy") if data.get("conflictPolicy") in CONFLICT_POLICIES
                              else CONFLICT_SKIP,
            "sourceSnapshot": bool(data.get("sourceSnapshot", False)),
            "snapshotVersion": SNAPSHOT_VERSION,
            "snapshot": {},
            "taskHistory": [],
        }
        with self._lock:
            self._bindings.append(binding)
        self.save()
        return self.get_binding(binding["id"])

    def update_binding(self, binding_id, data):
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return None
            if b.get("scanStatus") == SCAN_RUNNING:
                raise ValueError("正在扫描中，请稍后再修改")

            if "name" in data:
                b["name"] = (data.get("name") or "").strip() or b["name"]
            if "localPath" in data:
                raw_lp = (data.get("localPath") or "").strip()
                if not raw_lp:
                    raise ValueError("OpenList 源目录不能为空")
                lp = normalize_cloud(raw_lp)
                if lp != b.get("localPath"):
                    # 源目录变了 → 旧扫描结果与快照都作废
                    b["localPath"] = lp
                    b["files"] = []
                    b["snapshot"] = {}
                    b["lastScannedAt"] = 0
            if "cloudTargets" in data:
                targets = self._normalize_targets(data.get("cloudTargets") or [])
                if not targets:
                    raise ValueError("至少需要一个云端目录")
                # 只有目标集合真的变了才作废扫描结果：编辑名称 / 开关自动上传
                # 这类操作不该把用户看到的对比结果清空。
                if not self._targets_equal(b.get("cloudTargets") or [], targets):
                    b["cloudTargets"] = targets
                    b["files"] = []
                    b["lastScannedAt"] = 0
            if "scanDepth" in data:
                b["scanDepth"] = self._normalize_depth(data.get("scanDepth"))
            if "autoUpload" in data:
                b["autoUpload"] = bool(data.get("autoUpload"))
            if "excludeNames" in data:
                b["excludeNames"] = self._normalize_excludes(data.get("excludeNames"))
            if "syncMode" in data:
                b["syncMode"] = self._normalize_sync_mode(data.get("syncMode"))
            if "conflictPolicy" in data:
                cp = data["conflictPolicy"]
                if cp in CONFLICT_POLICIES:
                    b["conflictPolicy"] = cp
            if "sourceSnapshot" in data:
                b["sourceSnapshot"] = bool(data["sourceSnapshot"])
                if not b["sourceSnapshot"]:
                    b["snapshot"] = {}
            b["snapshotVersion"] = SNAPSHOT_VERSION
            b["updatedAt"] = _now()
        self.save()
        return self.get_binding(binding_id)

    def delete_binding(self, binding_id):
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return False
            if b.get("scanStatus") == SCAN_RUNNING:
                flag = self._scan_flags.get(binding_id)
                if flag:
                    flag.set()
            self._bindings = [x for x in self._bindings
                              if x.get("id") != binding_id]
        self.save()
        return True

    # -- 目标 / 规则规范化 --

    @staticmethod
    def _normalize_targets(targets_in):
        out = []
        seen = set()
        for t in targets_in:
            if isinstance(t, str):
                path = t.strip()
                enabled = True
                tid = _new_id("ct")
            elif isinstance(t, dict):
                path = (t.get("path") or "").strip()
                enabled = bool(t.get("enabled", True))
                tid = t.get("id") or _new_id("ct")
            else:
                continue
            if not path:
                continue
            # 规范化为 /a/b 形式
            path = "/" + "/".join(
                p for p in path.replace("\\", "/").split("/") if p)
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": str(tid), "path": path, "enabled": enabled})
        return out

    @staticmethod
    def _targets_equal(old, new):
        """两组云端目标是否等价（比 id / path / enabled，顺序无关）。"""
        def key(items):
            return sorted((str(t.get("id") or ""), str(t.get("path") or ""),
                           bool(t.get("enabled", True))) for t in items or [])
        return key(old) == key(new)

    @staticmethod
    def _normalize_sync_mode(raw):
        """只保留「只新增」。历史数据里的 full / move 没有实现过，
        而且会删用户的文件，一律降级为 add。"""
        return raw if raw in SYNC_MODES else SYNC_ADD

    @staticmethod
    def _normalize_depth(raw):
        if raw in (None, ""):
            return DEFAULT_BINDING_DEPTH
        try:
            d = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_BINDING_DEPTH
        if d < 0:
            return UNLIMITED_DEPTH
        return d

    @staticmethod
    def _normalize_excludes(raw):
        if raw is None:
            return list(DEFAULT_EXCLUDES)
        if isinstance(raw, str):
            items = [s.strip() for s in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            items = [str(s).strip() for s in raw]
        else:
            return list(DEFAULT_EXCLUDES)
        return [s for s in items if s]

    # ------------------------------------------------------------------
    # 增量扫描
    # ------------------------------------------------------------------

    def _has_inflight_upload(self, b):
        """该绑定是否有「排队中 / 传输中」的上传任务（含提交阶段）。

        上传进行中禁止扫描：扫描会把正在复制的文件判成缺失，若开了自动上传
        会在复制尚未完成时再次提交同一批文件，造成重复上传。
        """
        if self.is_submitting(b.get("id")):
            return True
        for f in (b.get("files") or []):
            for ts in (f.get("targets") or {}).values():
                if ts.get("status") in IN_FLIGHT_STATES:
                    return True
        return False

    def can_scan(self, binding_id):
        """扫描前置闸门：返回 (允许, 原因)。上传/提交进行中或已在扫描时不允许。"""
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return False, "绑定不存在"
            if b.get("scanStatus") == SCAN_RUNNING:
                return False, "该同步正在扫描中"
            if self._has_inflight_upload(b):
                return False, "该同步有上传任务进行中，请等待完成后再扫描"
        return True, ""

    def set_scan_concurrency(self, n):
        """设置全局扫描并发数。重建信号量（已在运行的不受影响，新任务按新限制排队）。"""
        n = max(1, min(10, int(n or 2)))
        if n == self._scan_concurrency:
            return
        self._scan_concurrency = n
        self._scan_semaphore = threading.Semaphore(n)
        self._log("全局扫描并发数设置为 %d" % n)

    def start_scan(self, binding_id, background=True):
        """启动增量扫描。background=True 时在后台线程执行，立即返回。

        上传/提交进行中拒绝扫描（见 can_scan），避免重复上传。
        """
        ok, reason = self.can_scan(binding_id)
        if not ok:
            if reason == "绑定不存在":
                return False
            # 已在扫描 / 上传进行中：跳过并记录，scan_all 与自动扫描据此静默跳过
            self._log("跳过扫描（%s）：%s" % (binding_id[:8], reason))
            return False
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return False
            b["scanStatus"] = SCAN_RUNNING
            b["scanError"] = ""
            b["scanProgress"] = {"phase": "starting", "scanned": 0, "current": ""}
            cancel_flag = threading.Event()
            self._scan_flags[binding_id] = cancel_flag
        self.save()

        if background:
            th = threading.Thread(target=self._scan_worker,
                                  args=(binding_id, cancel_flag),
                                  name="binding-scan-" + binding_id[:8],
                                  daemon=True)
            self._scan_threads[binding_id] = th
            th.start()
            return True
        self._scan_worker(binding_id, cancel_flag)
        return True

    def scan_all(self):
        """扫描所有绑定（后台）。有上传/提交任务进行中的绑定自动跳过，避免重复上传。

        返回 (started, skipped)：started=本次实际触发的数量，skipped=因扫描中或
        上传进行中而跳过的数量。
        """
        started = 0
        skipped = 0
        with self._lock:
            ids = [b["id"] for b in self._bindings]
        for bid in ids:
            if self.start_scan(bid):
                started += 1
            else:
                skipped += 1
        return started, skipped

    # -- 自动扫描：每隔 N 天在指定时间点执行全量扫描 --
    # 平时不做任何扫描，到点才对所有绑定做一次完整对比。
    # 间隔天数和执行时间点均可配置，默认每7天凌晨3点。

    def configure_auto_scan(self, enabled, days=7, schedule_time="03:00"):
        """按配置启停自动扫描。保存设置后立即调用，立即生效。"""
        self._auto_stop.set()
        if self._auto_timer is not None:
            try:
                self._auto_timer.cancel()
            except Exception:
                pass
            self._auto_timer = None
        if not enabled:
            return
        try:
            days = max(1, int(days))
        except Exception:
            days = 7
        self._auto_days = days
        self._schedule_time = schedule_time or "03:00"
        self._auto_stop.clear()
        interval = self._seconds_until_next_run(days, self._schedule_time)
        self._log("auto-scan configured: every %d days at %s, next run in %d seconds"
                  % (days, self._schedule_time, interval))
        self._schedule_auto(interval)

    def _seconds_until_next_run(self, days, time_str):
        """计算到下一个执行时间点的秒数：每隔 N 天，在指定时间点执行。

        策略：从今天的指定时间点开始，往后找第一个 >= 当前时间的执行点。
        如果今天的时间点已过，则加上 N 天。
        """
        try:
            h, m = [int(x) for x in str(time_str).split(":")[:2]]
        except Exception:
            h, m = 3, 0
        now = time.time()
        # 今天的指定时间点
        today = time.localtime(now)
        target_today = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                    h, m, 0, 0, 0, -1))
        if target_today >= now:
            return int(target_today - now)
        # 今天的时间点已过，N 天后的同一时间点
        target_future = target_today + days * 86400
        return int(target_future - now)

    def _schedule_auto(self, interval):
        self._auto_timer = threading.Timer(interval, self._auto_tick, args=(interval,))
        self._auto_timer.daemon = True
        self._auto_timer.start()

    def _auto_tick(self, interval):
        """定时全量扫描：到点对所有启用的绑定做一次完整对比。"""
        try:
            if self._auto_stop.is_set():
                return
            try:
                n, skipped = self.scan_all()
                if n:
                    self._log("auto scheduled scan triggered %d binding(s) (every %d days at %s)%s"
                              % (n, getattr(self, "_auto_days", 7),
                                 getattr(self, "_schedule_time", "03:00"),
                                 "，%d 个因上传中/扫描中跳过" % skipped if skipped else ""))
            except Exception as exc:
                self._log("auto full-scan error: %s" % exc)
            if not self._auto_stop.is_set():
                # 计算下一次执行时间。重新排期失败不能静默吞掉，
                # 否则定时扫描从此再也不跑，用户永远不知道。
                try:
                    next_interval = self._seconds_until_next_run(
                        getattr(self, "_auto_days", 7),
                        getattr(self, "_schedule_time", "03:00"))
                    self._schedule_auto(next_interval)
                except Exception as exc:
                    self._log("auto scan reschedule failed: %s, fallback to 24h" % exc)
                    # 兜底：24 小时后再试，避免定时器彻底死亡
                    self._schedule_auto(86400)
        except Exception as exc:
            self._log("auto tick unexpected error: %s" % exc)

    def _scan_worker(self, binding_id, cancel_flag):
        with self._lock:
            b = self._find(binding_id)
        if not b:
            return
        # 全局并发限制：获取信号量，超过限制的绑定在此排队等待
        b["scanProgress"] = {"phase": "queued", "scanned": 0, "current": "等待扫描槽位…"}
        self._scan_semaphore.acquire()
        try:
            if cancel_flag.is_set():
                b["scanStatus"] = SCAN_IDLE
                b["scanError"] = "扫描已取消"
                self.save()
                return
            b["scanProgress"] = {"phase": "starting", "scanned": 0, "current": ""}
            files, total = self._do_scan(b, cancel_flag)
            if cancel_flag.is_set():
                b["scanStatus"] = SCAN_IDLE
                b["scanError"] = "扫描已取消"
            else:
                b["files"] = files
                # 源目录里的文件总数（不是"需要处理的文件数"）
                b["totalLocalFiles"] = total
                b["lastScannedAt"] = _now()
                b["scanStatus"] = SCAN_DONE
                b["scanError"] = ""
                # 扫描结果统计
                stats = self._calc_scan_stats(files, total)
                b["scanStats"] = stats
                b["scanProgress"] = {"phase": "done", "scanned": total, "current": ""}
                self._log("绑定「%s」扫描完成：源目录 %d 个文件，%d 个待处理（缺失 %d，不一致 %d）"
                          % (b.get("name"), total, stats["pending"],
                             stats["missing"], stats["mismatch"]))
                self._record_history(b, "scan", {
                    "scanned": total,
                    "pending": stats["pending"],
                    "missing": stats["missing"],
                    "mismatch": stats["mismatch"],
                })
                self.save()
                # 自动上传
                if b.get("autoUpload") and not cancel_flag.is_set():
                    self._auto_upload(b)
                return
        except Exception as exc:
            b["scanStatus"] = SCAN_ERROR
            b["scanError"] = str(exc)
            self._log("绑定「%s」扫描失败：%s" % (b.get("name"), exc))
        finally:
            self._scan_semaphore.release()
        self.save()

    def _do_scan(self, b, cancel_flag):
        """核心扫描：OpenList 源目录 vs 每个目标目录。

        返回 `(files, total_source_files)`。files 只保留「有目标需要处理」或
        「正在复制中」的文件 —— 前者要展示待处理，后者要展示进度，已经同步好的
        文件没有任何信息量，不进列表。
        """
        local_path = b["localPath"]
        depth = b.get("scanDepth", DEFAULT_BINDING_DEPTH)
        # 注意：不能用 `or DEFAULT_EXCLUDES`，空列表是用户明确清空的结果，
        # 当成假值会让默认名单复活。
        excludes = b.get("excludeNames")
        if excludes is None:
            excludes = DEFAULT_EXCLUDES
        scan_workers = verify_mod.normalize_concurrency(
            config.load_config().get("scanConcurrency"))

        # 进度更新辅助：写 scanProgress 但不持锁（只是展示用，丢一两次无所谓）
        def _progress(phase, scanned=0, current=""):
            try:
                b["scanProgress"] = {"phase": phase, "scanned": scanned,
                                     "current": current}
            except Exception:
                pass

        # 1. 扫描 OpenList 源目录。源目录读不到就**别装作扫完了**：
        #    静默吞掉会让界面显示"已完全同步、无待处理"，而事实是根本没读到源。
        _progress("source", 0, local_path)
        local_map = verify_mod.scan_cloud(
            self.client, local_path, depth, excludes, concurrency=scan_workers,
            progress=lambda rel, cnt: _progress("source", cnt, rel))
        if cancel_flag.is_set():
            return [], 0

        total_source = sum(1 for info in local_map.values() if not info.get("isDir"))
        _progress("source", total_source, "")

        # 快照模式：记录本次源目录的文件指纹（大小 + 云端 modified）+ 各目标判定结果。
        # 下次扫描时，指纹未变的文件可以直接沿用「上次已同步」的结论，
        # 连 stat 都不用发 —— 这是「快照」真正省下来的东西。
        snapshot = b.get("snapshot") or {}
        use_snapshot = (bool(b.get("sourceSnapshot"))
                        and int(b.get("snapshotVersion") or 0) == SNAPSHOT_VERSION
                        and bool(snapshot))
        new_snapshot = {}
        if b.get("sourceSnapshot"):
            for rel, info in local_map.items():
                if info.get("isDir"):
                    continue
                new_snapshot[rel] = {
                    "size": int(info.get("size") or 0),
                    "modified": str(info.get("modified") or ""),
                }

        prev_files = {f.get("relPath"): f for f in b.get("files", [])}

        # 有在途复制的文件：状态推进要回云端核对，先按目标目录批量预取一次，
        # 免得在逐文件循环里各发一次单文件查询。
        inflight_pairs = []
        for rel, pf in prev_files.items():
            for target in b.get("cloudTargets", []):
                if not target.get("enabled"):
                    continue
                ts = (pf.get("targets") or {}).get(target.get("id"))
                if ts and ts.get("status") in IN_FLIGHT_STATES:
                    inflight_pairs.append((rel, target))
        resolve_lookups = self._build_lookups(inflight_pairs) if inflight_pairs else {}

        # 2. 找出"源文件变了吗"。快照模式下只有变了的文件才需要去目标端核对。
        changed = None                       # None = 全量模式
        if use_snapshot:
            changed = set()
            for rel, info in local_map.items():
                if info.get("isDir"):
                    continue
                old = snapshot.get(rel) or {}
                if (int(old.get("size") or -1) != int(info.get("size") or 0)
                        or str(old.get("modified") or "")
                        != str(info.get("modified") or "")):
                    changed.add(rel)
            self._log("绑定「%s」快照扫描：源 %d 个文件，%d 个新增/变化"
                      % (b.get("name"), total_source, len(changed)))

        # 3. 扫目标目录（全量模式才需要整树扫描）
        enabled_targets = [t for t in b.get("cloudTargets", []) if t.get("enabled")]
        target_maps = {}
        lookups = {}
        if not use_snapshot:
            for ti, target in enumerate(enabled_targets):
                if cancel_flag.is_set():
                    break
                _progress("target", 0, "%s (%d/%d)" % (target.get("path"), ti+1, len(enabled_targets)))
                try:
                    target_maps[target["id"]] = verify_mod.scan_cloud(
                        self.client, target["path"], depth, excludes,
                        concurrency=scan_workers,
                        progress=lambda rel, cnt, t=target: _progress(
                            "target", cnt, "%s: %s" % (t.get("path"), rel)))
                except Exception as exc:
                    # 单个目标扫描失败不阻塞其他目标
                    target_maps[target["id"]] = {"__error__": str(exc)}
        else:
            # 快照模式：没有整树结果，但**也不要退化成"每文件一次 stat"**。
            # 先把「需要去目标端核对」的文件挑出来（变化过的 + 快照里还没定论的），
            # 交给 CloudEntryLookup 按父目录批量、并发地列。
            need_lookup = set(changed or ())
            snapshot_targets = {}
            for rel, info in local_map.items():
                if info.get("isDir"):
                    continue
                snaps = ((snapshot.get(rel) or {}).get("targets") or {})
                snapshot_targets[rel] = snaps
                for target in enabled_targets:
                    ts = snaps.get(target["id"])
                    if not ts or ts.get("status") not in SETTLED_STATES:
                        need_lookup.add(rel)
                        break
            for ti, target in enumerate(enabled_targets):
                if cancel_flag.is_set():
                    break
                _progress("target", 0, "%s (%d/%d)" % (target.get("path"), ti+1, len(enabled_targets)))
                lookup = CloudEntryLookup(self.client, target.get("path"),
                                          concurrency=scan_workers, log=self._log)
                lookup.prefetch(need_lookup)
                lookups[target["id"]] = lookup
            self._log("绑定「%s」快照扫描：%d 个文件需核对目标端，按目录批量查询"
                      % (b.get("name"), len(need_lookup)))

        # 4. 逐文件对比
        tasks = None                    # 任务列表按需拉一次（快照模式下可能不需要）
        files = []
        compared = 0
        for rel_path in sorted(local_map.keys()):
            local_info = local_map[rel_path]
            if local_info.get("isDir"):
                continue
            if cancel_flag.is_set():
                break
            compared += 1
            if compared % 50 == 0:
                _progress("compare", compared, rel_path)

            prev_targets = (prev_files.get(rel_path) or {}).get("targets") or {}
            targets_status = {}
            needs_action = False
            in_flight = False

            for target in b.get("cloudTargets", []):
                tid = target["id"]
                if not target.get("enabled"):
                    continue
                prev = prev_targets.get(tid)

                # ① 正在复制中的，按任务/云端实际结果推进状态
                if prev and prev.get("status") in IN_FLIGHT_STATES:
                    if tasks is None:
                        tasks = self._fetch_tasks()
                    synced = self._resolve_inflight(
                        {"relPath": rel_path, "size": local_info.get("size")}, target, prev, tasks,
                        lookup=self._lookup_for(resolve_lookups, target))
                    targets_status[tid] = synced
                    if synced.get("status") in PENDING_STATES:
                        needs_action = True
                    elif synced.get("status") in IN_FLIGHT_STATES:
                        in_flight = True
                    continue

                # ② 快照模式下、源文件没变、且上次已确认同步 → 直接沿用结论，不发请求
                if use_snapshot and rel_path not in changed:
                    old_ts = ((snapshot.get(rel_path) or {}).get("targets") or {}).get(tid)
                    if old_ts and old_ts.get("status") in SETTLED_STATES:
                        targets_status[tid] = dict(old_ts)
                        continue

                # ③ 其余情况：拿目标端实际内容比对
                target_error = (target_maps.get(tid) or {}).get("__error__")
                if target_error:
                    targets_status[tid] = {
                        "status": ST_FAILED,
                        "cloudSize": 0,
                        "error": "云端扫描失败：%s" % target_error,
                        "taskId": "",
                    }
                    needs_action = True
                    continue
                if use_snapshot:
                    # 快照模式没有整树结果，改走按父目录批量查询（等价于逐文件 stat，
                    # 但把请求数从"文件数"降到"目录数"）
                    lookup = lookups.get(tid)
                    cloud_info = lookup.get(rel_path) if lookup else _UNKNOWN
                else:
                    cloud_info = self._cloud_entry(target_maps.get(tid), rel_path,
                                                   target)
                if cloud_info is _UNKNOWN:
                    # 「目标端查不出来」（网络/权限）≠「云端没有这个文件」。
                    # 拿不到确凿证据就沿用上次结论；连上次结论都没有时，
                    # 按待处理处理（宁可多核对一次，也不能谎报已同步）。
                    fallback = (snapshot_targets.get(rel_path, {}).get(tid)
                                or prev_targets.get(tid))
                    if fallback and fallback.get("status"):
                        kept = dict(fallback)
                        if not kept.get("error"):
                            kept["error"] = "云端查询失败，沿用上次结论"
                        targets_status[tid] = kept
                        if kept.get("status") in PENDING_STATES:
                            needs_action = True
                        elif kept.get("status") in IN_FLIGHT_STATES:
                            in_flight = True
                    else:
                        targets_status[tid] = {
                            "status": ST_MISSING, "cloudSize": 0,
                            "error": "云端查询失败，未取得该文件的实际状态",
                            "taskId": "",
                        }
                        needs_action = True
                    continue
                if cloud_info is None:
                    targets_status[tid] = {
                        "status": ST_MISSING, "cloudSize": 0,
                        "error": "", "taskId": "",
                    }
                    needs_action = True
                elif cloud_info.get("isDir"):
                    targets_status[tid] = {
                        "status": ST_MISMATCH, "cloudSize": 0,
                        "error": "云端同路径是目录，不是文件", "taskId": "",
                    }
                    needs_action = True
                elif int(cloud_info.get("size") or 0) != int(local_info.get("size") or 0):
                    targets_status[tid] = {
                        "status": ST_MISMATCH,
                        "cloudSize": int(cloud_info.get("size") or 0),
                        "error": "", "taskId": "",
                    }
                    needs_action = True
                else:
                    targets_status[tid] = {
                        "status": ST_UPLOADED,
                        "cloudSize": int(cloud_info.get("size") or 0),
                        "error": "", "taskId": "",
                    }

            if needs_action or in_flight:
                files.append({
                    "relPath": rel_path,
                    "name": rel_path.split("/")[-1],
                    "size": int(local_info.get("size") or 0),
                    "mtime": int(local_info.get("mtime") or 0),
                    "targets": targets_status,
                })
            if b.get("sourceSnapshot"):
                new_snapshot.setdefault(rel_path, {})
                new_snapshot[rel_path]["targets"] = _copy_json(targets_status)

        # 扫描被取消时不落盘快照：只填了一半的快照会让下次扫描误以为"其余文件都没变"。
        if b.get("sourceSnapshot") and not cancel_flag.is_set():
            b["snapshot"] = new_snapshot
            b["snapshotVersion"] = SNAPSHOT_VERSION
        return files, total_source

    def _cloud_entry(self, cloud_map, rel_path, target):
        """从**整树扫描**的结果里取某目标上 rel_path 的条目（全量模式用）。

        快照模式没有整树结果，走 `CloudEntryLookup`（按父目录批量列），不走这里。
        """
        if not cloud_map or "__error__" in cloud_map:
            return None
        return cloud_map.get(rel_path)

    # ------------------------------------------------------------------
    # 复制结果判定
    # ------------------------------------------------------------------

    def _fetch_tasks(self):
        """拉一次 OpenList 复制任务列表。拿不到返回 None（**不等于没有任务**）。

        刻意**不做时间缓存**：调用点全都「先确认有在途任务，才来拉任务列表」
        （没有在途任务时根本不会调用），也就是每次调用都正是最需要新鲜结果的时刻。
        加一层 1.5 秒 TTL 试过：一次请求都省不下来（前端轮询间隔本来就 > TTL），
        却会把「任务已被清理 → 该判缺失」这类状态转换推迟到缓存过期之后。
        """
        fn = getattr(self.client, "list_tasks", None)
        if fn is None:
            return None
        try:
            try:
                return fn("copy")
            except TypeError:
                return fn()
        except Exception as exc:
            self._log("查询 OpenList 复制任务失败：%s" % exc)
            return None

    @staticmethod
    def _find_task(tasks, task_id):
        if not tasks or not task_id:
            return None, None
        for bucket in ("undone", "done"):
            for tk in tasks.get(bucket) or []:
                if str(tk.get("id") or "") == str(task_id):
                    return bucket, tk
        return None, None

    @staticmethod
    def _queued_stale(ts):
        """这条「进行中」是否已经久到可以下结论了。

        `queuedAt` 缺失（极旧数据）视为「很久以前」，与既有行为保持一致。
        """
        queued_at = int((ts or {}).get("queuedAt") or 0)
        return (not queued_at) or (_now() - queued_at > QUEUED_STALE_SECONDS)

    def _build_lookups(self, pairs):
        """按「目标根目录」把待核对的文件一次性预取，返回 {目标根: CloudEntryLookup}。

        传输列表与绑定状态都是每 2 秒刷新一次，而进度核对必须回云端看文件到底
        落地没有。逐文件 `stat` 的话，刚点完「一键上传」几百个文件时，每一轮刷新
        都会打出几百次单文件查询 —— 接口被拖慢，界面就表现为「点了没反应」。
        按父目录批量列之后，请求数从「文件数」降到「目录数」。
        """
        buckets = {}
        for rel, target in pairs or ():
            buckets.setdefault(target.get("path") or "/", set()).add(rel)
        lookups = {}
        for base, rels in buckets.items():
            lookup = CloudEntryLookup(self.client, base, log=self._log)
            lookup.prefetch(rels)
            lookups[base] = lookup
        return lookups

    @staticmethod
    def _lookup_for(lookups, target):
        if not lookups:
            return None
        return lookups.get(target.get("path") or "/")

    def _verify_cloud(self, f, target, ts, lookup=None):
        """回云端核对目标文件。返回 (status, cloudSize, error)；查不了返回 None。

        只有这一步（或 OpenList 任务明确报成功）才能得出 uploaded。
        `lookup` 非 None 时走「按父目录批量列」，一次请求核对同目录的一批文件；
        否则退回逐文件 `stat`。
        """
        dst = join_cloud(target.get("path"), f.get("relPath"))
        if lookup is not None:
            node = lookup.get(f.get("relPath"))
            if node is _UNKNOWN:
                return None
        else:
            try:
                node = self.client.stat(dst)
            except Exception as exc:
                self._log("核对云端文件失败（%s）：%s" % (dst, exc))
                return None
        if node is None:
            return ST_MISSING, 0, ""
        if node.get("is_dir"):
            return ST_MISMATCH, 0, "云端同路径是目录，不是文件"
        size = int(node.get("size") or 0)
        if size == int(f.get("size") or 0):
            return ST_UPLOADED, size, ""
        return ST_MISMATCH, size, ""

    def _finish_from_cloud(self, f, target, ts, gone=False, lookup=None):
        """用云端实际内容给一条进行中的状态收尾。

        查不到云端（网络/权限问题）时原样返回，绝不猜测成功。

        `gone=True` 只说明「按任务 id 在 OpenList 任务列表里没找到」，这**不等于**
        这次复制没成：OpenList 完成任务后会清理任务记录，而且跨存储复制返回的
        `data["tasks"]` 里的 id 与 `/api/task/copy/*` 里的 id 未必同源。所以只要
        任务查不到就把它判回缺失，会造成两个后果：

        * 刚提交的任务立刻从传输列表消失、绑定卡片又变回「缺失」——看起来就像
          「点了没反应」，而 OpenList 那边其实正在传；
        * 真正在复制的文件被判成缺失，用户重传后变成重复上传。

        因此这里统一遵守 `QUEUED_STALE_SECONDS` 宽限期：只有等了足够久仍拿不到
        文件，才用云端定论收尾。
        """
        new = dict(ts)
        verdict = self._verify_cloud(f, target, ts, lookup=lookup)
        if verdict is None:
            if gone and self._queued_stale(ts):
                new.update(status=ST_FAILED, taskId="", error="复制结果无法确认，请重新上传")
            return new
        status, size, err = verdict
        if status == ST_UPLOADED:
            new.update(status=ST_UPLOADED, cloudSize=size, taskId="",
                       error="", progress=100, doneAt=_now())
            return new
        # 复制还没落地（同存储同步复制也有一瞬间）→ 保持进行中，下次再看
        if not self._queued_stale(ts):
            return new
        new.update(status=status, cloudSize=size, taskId="",
                   error=err or ("复制任务已结束，但云端文件与源不一致" if gone
                                 else "长时间未确认复制结果，请重新上传"))
        return new

    def _resolve_inflight(self, f, target, ts, tasks, lookup=None):
        """把一条 queued/uploading 的状态推进到最新。

        证据优先级：OpenList 任务状态 > 云端文件实际内容。两者都拿不到就原样返回。
        `lookup` 见 `_build_lookups`：批量核对同一个目标目录下的多个文件。
        """
        tid = ts.get("taskId") or ""
        if tid and tasks is not None:
            bucket, tk = self._find_task(tasks, tid)
            if bucket == "undone":
                new = dict(ts)
                # OpenList state：0=Pending 只是在服务端排队，还没开始复制；
                # 1=Running 才真正在跑。一律标「上传中」会让用户对着
                # 「上传中 0%」干等 —— 那些任务其实一个字节都没动。
                state = int(tk.get("state") or 0)
                new["status"] = ST_UPLOADING if state >= 1 else ST_QUEUED
                new["progress"] = tk.get("progress") or 0
                return new
            if bucket == "done":
                state = int(tk.get("state") or 0)
                if state == openlist.TASK_STATE_SUCCEEDED:
                    return self._finish_from_cloud(f, target, ts, lookup=lookup)
                if state in openlist.TASK_STATE_FAILED:
                    new = dict(ts)
                    new.update(status=ST_FAILED, taskId="",
                               error=tk.get("error") or "OpenList 复制任务失败（state=%d）" % state)
                    return new
                new = dict(ts)
                new["status"] = ST_UPLOADING
                new["progress"] = tk.get("progress") or 0
                return new
            # 任务在所有列表里都找不到（被清理 / 服务端重启 / id 不同源）
            # → 用云端定论，但同样受 QUEUED_STALE_SECONDS 宽限期保护
            return self._finish_from_cloud(f, target, ts, gone=True, lookup=lookup)
        # 没有 taskId（同存储复制不建任务），或任务列表本轮没查到：
        # 只能看云端，且不因此判定失败
        return self._finish_from_cloud(f, target, ts, gone=False, lookup=lookup)

    @staticmethod
    def _count_pending(files):
        count = 0
        for f in files:
            for s in (f.get("targets") or {}).values():
                if s.get("status") in PENDING_STATES:
                    count += 1
                    break
        return count

    @staticmethod
    def _calc_scan_stats(files, total_source):
        """计算扫描结果统计：缺失、不一致、已同步、待处理、在途数量。"""
        missing = 0
        mismatch = 0
        uploaded = 0
        inflight = 0
        failed = 0
        pending = 0
        for f in files:
            targets = f.get("targets") or {}
            file_pending = False
            for s in targets.values():
                st = s.get("status")
                if st == ST_MISSING:
                    missing += 1
                    file_pending = True
                elif st == ST_MISMATCH:
                    mismatch += 1
                    file_pending = True
                elif st in SETTLED_STATES:
                    uploaded += 1
                elif st in IN_FLIGHT_STATES:
                    inflight += 1
                elif st == ST_FAILED:
                    failed += 1
                    file_pending = True
            if file_pending:
                pending += 1
        # 已同步的文件不在 files 列表里（只保留待处理和在途），
        # 所以已同步数 = 源文件总数 - 待处理数 - 在途数
        synced = max(0, total_source - pending - inflight)
        return {
            "total": total_source,
            "missing": missing,
            "mismatch": mismatch,
            "failed": failed,
            "inflight": inflight,
            "pending": pending,
            "synced": synced,
        }

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------

    def upload_files(self, binding_id, rel_paths=None, target_ids=None,
                     upload_all=False):
        """把缺失文件通过 OpenList 服务端复制提交到目标。

        rel_paths: 指定文件（相对路径列表）；None + upload_all=True 时上传全部。
        target_ids: 只上传到指定目标；None 时上传到所有缺失目标。
        返回实际提交复制的文件数。

        整个过程会持续刷新 `_submit[binding_id]`，前端据此画进度条 ——
        这个动作可能要跑十几秒，没有进度就等于「点了没反应」。
        """
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return None

        # 原子幂等闸门：检查 + 登记在同一把锁里完成，避免 TOCTOU。
        # 前端的按钮禁用只是体验层，这里才是真正的保证：双击、
        # 两个标签页同时点、提交期间又触发一次自动上传，都会走到这儿。
        # 没有它，同一批文件会被重复提交（服务端白跑一遍复制，还可能
        # 与正在进行的任务互相打架）。
        if not self._submit_try_begin(binding_id):
            raise ValueError("该绑定正在提交上传，请稍候再试")

        try:
            return self._upload_files_inner(b, rel_paths=rel_paths,
                                            target_ids=target_ids,
                                            upload_all=upload_all)
        except Exception as exc:
            # 异常路径也必须收尾：否则这个绑定会永久卡在「正在提交」，
            # 此后所有上传请求都被幂等闸门挡回去 —— 一次网络抖动就废掉一个绑定。
            self._submit_end(binding_id, phase=SUBMIT_FAILED,
                             message="提交中断：%s" % exc)
            raise

    def _upload_files_inner(self, b, rel_paths=None, target_ids=None,
                            upload_all=False):
        """`upload_files` 的正体。调用方负责 `_submit_begin` / 异常收尾。"""
        binding_id = b.get("id")
        local_base = b["localPath"]
        # 按 (src_dir, dst_dir) 分组，一次 copy 同目录多个文件
        groups = {}  # (src_dir, dst_dir) -> [(rel, tid, name)]

        # 用集合匹配：对一个文件夹点「一键上传」时，这里要拿每个文件去比对传来的
        # 路径清单，列表线性查找在几千个文件时会明显卡顿（点下去像"没反应"）。
        wanted = None if rel_paths is None else {str(x) for x in rel_paths}
        for f in b.get("files", []):
            rel = f["relPath"]
            if wanted is not None and rel not in wanted:
                continue
            if not upload_all and wanted is None:
                continue
            for target in b.get("cloudTargets", []):
                tid = target["id"]
                if not target.get("enabled"):
                    continue
                if target_ids and tid not in target_ids:
                    continue
                ts = (f.get("targets") or {}).get(tid)
                if not ts or ts.get("status") not in PENDING_STATES:
                    continue
                parts = rel.split("/")
                name = parts[-1]
                sub = "/".join(parts[:-1])
                src_dir = local_base.rstrip("/") + ("/" + sub if sub else "")
                dst_dir = target["path"].rstrip("/") + ("/" + sub if sub else "")
                key = (src_dir, dst_dir)
                groups.setdefault(key, []).append((rel, tid, name))

        if not groups:
            self._submit_end(binding_id, message="没有需要上传的文件")
            return 0

        # 分组数 = 要发多少轮「ensure_dir + copy」。把这个分母告诉前端，
        # 进度条才是有意义的（而不是永远转圈）。
        involved_targets = set()
        for items in groups.values():
            for _rel, tid, _name in items:
                involved_targets.add(tid)
        self._submit_update(binding_id, phase=SUBMIT_SENDING,
                            total=len(groups),
                            files=sum(len(v) for v in groups.values()),
                            targets=len(involved_targets))

        conflict = b.get("conflictPolicy") if b.get("conflictPolicy") in CONFLICT_POLICIES \
            else CONFLICT_SKIP
        flags = CONFLICT_FLAGS[conflict]

        # 逐组提交原来是**串行**的：每个子目录两次网络往返（ensure_dir + copy），
        # 选一个含三十个子目录的文件夹就是六十次串行等待 —— 这就是「点下去要等
        # 很久」的绝对值来源。改成并发后，总耗时大致降到 1/并发路数。
        #
        # 并发路数复用用户已有的「扫描并发数」设置：都是「同时向 OpenList 发几路
        # 请求」这同一个旋钮，OpenList 在远端或有限流时调小即可，不必再多一个
        # 配置项让人犹豫该动哪个。
        workers = verify_mod.normalize_concurrency(
            config.load_config().get("scanConcurrency"))

        # 目录缓存：一次提交内**所有 worker 共享**。
        #
        # 没有它，8 路并发会对同一批父目录各查一遍，而 `ensure_dir` 自己还要
        # 从根到叶逐级查 —— 实测提交 30 个子目录 / 120 个文件时发了 177 次
        # 请求（30 次 copy + 90 次 stat + 57 次 mkdir），其中 54 次 stat 和
        # 21 次 mkdir 是纯粹的重复；而 OpenList 网页手动复制只要 1 次。
        dir_cache = openlist.DirCache()

        def submit_one(pair):
            """提交一个目录组。异常装进结果返回，不往上抛 ——
            否则一次失败会中断整批（`map_concurrent` 会把它记进结果列表）。"""
            _idx, ((src_dir, dst_dir), items) = pair
            names = [it[2] for it in items]
            ensure_err = None
            try:
                # ensure_dir 失败不再静默吞掉：记录日志，并在 copy 也失败时把
                # 建目录的错误附加上，避免用户只看到"复制失败"却不知道根因。
                # OpenList 的 copy 可能自动建目录，所以 ensure_dir 失败不中断，
                # 继续尝试 copy；只有 copy 也失败时才把 ensure_dir 的错报出来。
                try:
                    self.client.ensure_dir(dst_dir, dir_cache)
                except Exception as exc:
                    ensure_err = exc
                    self._log("确保目标目录存在失败（%s）：%s；将继续尝试复制" % (dst_dir, exc))
                data = self.client.copy(src_dir, dst_dir, names, **flags)
            except Exception as exc:
                # copy 失败时，如果 ensure_dir 也失败了，把两个错误都报出来
                if ensure_err is not None:
                    msg = "建目录失败(%s)，随后复制也失败：%s" % (ensure_err, exc)
                    exc = type(exc)(msg) if hasattr(exc, "__class__") else Exception(msg)
                return (src_dir, dst_dir, items, None, exc)
            finally:
                # 这一组无论成败都算「处理过了」，进度立刻前进一格。
                self._submit_bump(binding_id)
            return (src_dir, dst_dir, items, data, None)

        # 结果按输入顺序返回，汇总逻辑与串行版逐字一致 —— 这样才能保证
        # 「并发路数=1」与原来的行为完全等价（测试会两种都跑）。
        results = verify_mod.map_concurrent(
            submit_one, list(enumerate(groups.items())), workers)

        submitted = 0
        errors = []
        # 提交成功的 (rel, tid) → 服务端返回的真实任务 id（同存储复制没有任务，
        # 留空由云端核对定论；**不再靠"running 列表第一条"去猜**，那会把别的任务
        # 认成自己的，进而把没传成的文件标成已完成）
        submitted_keys = {}
        for res in results:
            if isinstance(res, BaseException):
                # 正常情况下 worker 自己已经兜住了异常，这里是防意料之外的逃逸。
                errors.append("提交线程异常：%s" % res)
                continue
            src_dir, dst_dir, items, data, exc = res
            if exc is not None:
                msg = str(exc).lower()
                # 判断"目标已存在"：必须是明确的冲突错误，不能用 "exist" 子串
                # （"does not exist" / "path not exist" / "failed get ... not exist"
                #  也包含 exist，但含义是源/父目录不存在，当成跳过成功会静默丢数据）
                is_conflict = (
                    "already exist" in msg
                    or "file exists" in msg
                    or "文件已存在" in str(exc)
                    or "目标已存在" in str(exc)
                    or (hasattr(exc, "code") and exc.code in (403, 409))
                )
                is_not_exist = (
                    "does not exist" in msg
                    or "not exist" in msg
                    or "no such file" in msg
                    or "不存在" in str(exc)
                )
                if is_conflict and conflict != CONFLICT_ABORT:
                    # 目标已存在且策略是跳过/覆盖（服务端未支持这两个开关时的兜底）
                    submitted += len(items)
                    for rel, tid, _name in items:
                        submitted_keys[(rel, tid)] = ""
                    continue
                if is_conflict:
                    errors.append("冲突（文件已存在）：%s -> %s" % (src_dir, dst_dir))
                    continue
                if is_not_exist:
                    errors.append("源或父目录不存在：%s -> %s: %s" % (src_dir, dst_dir, exc))
                    continue
                errors.append("%s -> %s: %s" % (src_dir, dst_dir, exc))
                continue
            submitted += len(items)
            task_ids = [str(tk.get("id") or "")
                        for tk in ((data or {}).get("tasks") or [])
                        if isinstance(tk, dict)]
            task_ids = [t for t in task_ids if t]
            # 只有当服务端为每个名字都建了任务时，顺序对应才可靠；
            # 数量对不上（有文件被跳过/同步完成）就一律不认领任务 id。
            if len(task_ids) == len(items):
                for (rel, tid, _name), task_id in zip(items, task_ids):
                    submitted_keys[(rel, tid)] = task_id
            else:
                for rel, tid, _name in items:
                    submitted_keys[(rel, tid)] = ""

        self._submit_update(binding_id, phase=SUBMIT_FINALIZE,
                            submitted=submitted, failed=len(errors))

        now = _now()
        with self._lock:
            for f in b.get("files", []):
                rel = f["relPath"]
                for target in b.get("cloudTargets", []):
                    tid = target["id"]
                    if (rel, tid) in submitted_keys:
                        f.setdefault("targets", {})[tid] = {
                            "status": ST_QUEUED,
                            "cloudSize": 0,
                            "error": "",
                            "taskId": submitted_keys[(rel, tid)],
                            "queuedAt": now,
                        }
        if errors:
            self._log("复制提交部分失败：%s" % "; ".join(errors))
        # 记录历史（放在 save 之前，否则这条历史要等下次落盘才写进磁盘）
        self._record_history(b, "upload", {
            "copied": submitted,
            "failed": len(errors),
            "mode": b.get("syncMode") if b.get("syncMode") in SYNC_MODES else SYNC_ADD,
        })
        self.save()
        if not submitted and errors:
            # 一个都没交出去：交给外层统一标记为「提交失败」并原样抛出原因。
            raise ValueError("复制失败：%s" % errors[0])
        # 部分失败**不算**「提交动作失败」—— 多数文件已经交出去了，失败明细放进
        # message 就够了。前端的进度条因此不会因为一个子目录出错就整条变红。
        self._submit_end(binding_id,
                         message=("%d 个目录提交失败：%s" % (len(errors), errors[0]))
                                 if errors else "")
        return submitted

    def _auto_upload(self, b):
        """扫描完成后自动上传所有缺失文件。"""
        try:
            count = self.upload_files(b["id"], upload_all=True)
            self._log("绑定「%s」自动上传 %d 个任务"
                      % (b.get("name"), count or 0))
        except ValueError as exc:
            # 最典型的是「该绑定正在提交上传」—— 用户手动点的一键上传还没跑完，
            # 扫描恰好结束。这不是故障，跳过就行，下次扫描还会自动上传。
            self._log("绑定「%s」自动上传跳过：%s" % (b.get("name"), exc))
        except Exception as exc:
            self._log("绑定「%s」自动上传失败：%s"
                      % (b.get("name"), exc))

    # ------------------------------------------------------------------
    # 状态刷新（前端轮询时调用）
    # ------------------------------------------------------------------

    def refresh_statuses(self, binding_id=None):
        """同步所有绑定文件的复制状态，返回更新后的绑定。

        网络请求一律在锁外、且每轮只拉一次任务列表 —— 之前是「持锁 + 每个目标
        各拉一次」，绑定一多就把整个接口堵住。
        """
        with self._lock:
            targets = self._bindings if binding_id is None else \
                [b for b in self._bindings if b.get("id") == binding_id]
            # 先收集需要判定的 (文件, 目标, 状态)，然后放锁
            jobs = []
            for b in targets:
                for f in b.get("files", []):
                    for tid, ts in (f.get("targets") or {}).items():
                        if ts.get("status") in IN_FLIGHT_STATES:
                            jobs.append((f, b, tid, ts))
        if not jobs:
            return self.get_binding(binding_id) if binding_id else self.list_bindings()

        tasks = self._fetch_tasks()
        # 待核对的文件按目标目录批量预取：这一步在锁外做，避免逐文件 stat
        pairs = []
        for f, b, tid, _ts in jobs:
            target = next((t for t in b.get("cloudTargets", [])
                           if t.get("id") == tid), None)
            if target is not None:
                pairs.append((f.get("relPath") or "", target))
        lookups = self._build_lookups(pairs)
        changed = False
        with self._lock:
            for f, b, tid, ts in jobs:
                target = next((t for t in b.get("cloudTargets", [])
                               if t.get("id") == tid), None)
                if target is None:
                    continue
                # 状态可能已被别的请求改过，以锁内的当前值为准
                live = (f.get("targets") or {}).get(tid)
                if not live or live.get("status") not in IN_FLIGHT_STATES:
                    continue
                new_ts = self._resolve_inflight(f, target, live, tasks,
                                                lookup=self._lookup_for(lookups, target))
                if new_ts != live:
                    f["targets"][tid] = new_ts
                    changed = True
            if changed:
                self.save()
        if binding_id:
            return self.get_binding(binding_id)
        return self.list_bindings()

    # -- 任务历史 --

    def _record_history(self, b, action, stats):
        """记录一条任务历史。"""
        h = {
            "id": _new_id("th"),
            "at": _now(),
            "action": action,
            "stats": stats,
        }
        hist = b.get("taskHistory") or []
        hist.append(h)
        # 只保留最近 50 条
        b["taskHistory"] = hist[-50:]

    def get_progress(self, binding_id, submit_only=False):
        """查询绑定上传的进度。

        submit_only=True 时**只**返回「提交阶段」的进度，且不发任何网络请求 ——
        前端在点完「一键上传」后的那几秒要每秒刷好几次进度条，每次都去问
        OpenList 的任务列表纯属浪费（提交还没完成时，任务列表里本来也没东西）。
        """
        # 查询参数经 `parse_qs` 解析后是 list，先规整 —— 否则 `_find` 永远查不到，
        # 接口会一直返回 progress:null，前端那个进度轮询等于白转。
        binding_id = self._submit_key(binding_id)
        submit = self._submit_view(binding_id)
        if submit_only:
            return {"running": [], "hasActive": False, "error": "",
                    "submit": submit}
        with self._lock:
            b = self._find(binding_id)
            if not b:
                return None
        tasks = self._fetch_tasks()
        result = {"running": [], "hasActive": False, "error": "", "submit": submit}
        if tasks is None:
            result["error"] = "查询 OpenList 任务列表失败（不影响复制本身）"
            return result
        for t in tasks.get("undone") or []:
            result["running"].append({
                "name": t.get("name") or "",
                "progress": t.get("progress") or 0,
                "total": t.get("total_bytes") or 0,
                "error": t.get("error") or "",
                "status": t.get("status") or "",
            })
        result["hasActive"] = len(result["running"]) > 0
        return result

    def list_binding_tasks(self):
        """返回传输列表里的绑定任务：进行中的，以及刚完成的。

        进行中 = queued / uploading / failed；刚完成 = 本绑定上传过（有提交或完成
        时间戳）且仍在 `BINDING_DONE_VISIBLE_SECONDS` 保留窗口内的 uploaded。
        """
        with self._lock:
            jobs = []
            for b in self._bindings:
                # 提交还在跑的时候，云端此刻必然还没有这些文件 —— 把几十个目标
                # 目录列一遍纯属白费，而且这些请求会和提交本身抢 OpenList，
                # 让「提交」显得更慢。列表照常渲染（下面的 result 循环覆盖全部
                # 状态），只是这一轮不去云端核对。
                if self.is_submitting(b.get("id")):
                    continue
                for f in b.get("files", []):
                    for tid, ts in (f.get("targets") or {}).items():
                        if ts.get("status") in IN_FLIGHT_STATES:
                            jobs.append((f, b, tid, ts))
        # 注意：jobs 为空**不能**直接 return []。刚完成的文件还要以「已完成」留在
        # 列表里，只是不需要再去拉任务列表、也不必批量核对云端（省一次请求）。
        if jobs:
            tasks = self._fetch_tasks()
            # 关键：待核对的文件按目标目录批量预取。这个接口是传输列表每 2 秒轮询的
            # 热路径，逐文件 stat 会让「刚点完一键上传」的那几秒直接卡住。
            pairs = []
            for f, b, tid, _ts in jobs:
                target = next((t for t in b.get("cloudTargets", [])
                               if t.get("id") == tid), None)
                if target is not None:
                    pairs.append((f.get("relPath") or "", target))
            lookups = self._build_lookups(pairs)
        else:
            tasks = {}
            lookups = {}
        changed = False
        with self._lock:
            for f, b, tid, ts in jobs:
                target = next((t for t in b.get("cloudTargets", [])
                               if t.get("id") == tid), None)
                if target is None:
                    continue
                live = (f.get("targets") or {}).get(tid)
                if not live or live.get("status") not in IN_FLIGHT_STATES:
                    continue
                new_ts = self._resolve_inflight(f, target, live, tasks,
                                                lookup=self._lookup_for(lookups, target))
                if new_ts != live:
                    f["targets"][tid] = new_ts
                    changed = True
            if changed:
                self.save()

            result = []
            finished = []
            for b in self._bindings:
                bname = b.get("name") or b.get("localPath") or ""
                for f in b.get("files", []):
                    for tid, ts in (f.get("targets") or {}).items():
                        st = ts.get("status")
                        done_at = ts.get("doneAt") or ts.get("queuedAt") or 0
                        # 「刚完成」的判定：本绑定上传过（有提交/完成时间）且还在保留窗口内。
                        # 只认有上传痕迹的那些 —— 扫描时云端本来就有、从未经过本绑定上传的
                        # 文件没有这两个时间戳，不该混进传输列表。
                        recent_done = (st in SETTLED_STATES and done_at
                                       and (_now() - done_at) <= BINDING_DONE_VISIBLE_SECONDS)
                        if st not in (ST_QUEUED, ST_UPLOADING, ST_FAILED) and not recent_done:
                            continue
                        target = next((t for t in b.get("cloudTargets", []) if t.get("id") == tid), {})
                        rel = f.get("relPath", "")
                        sub = "/".join(rel.split("/")[:-1])
                        root = (target.get("path") or "").rstrip("/")
                        row = {
                            "id": "bd_%s_%s_%s" % (b.get("id"), tid, rel),
                            # 前端要靠它把这类任务和上传队列的任务区分开：
                            # 绑定任务是 OpenList 服务端复制，id 不在上传队列里，
                            # 「取消 / 重传 / 删除」那些按钮对它一律无效。
                            "bindingId": b.get("id"),
                            "isBinding": True,
                            "name": rel.split("/")[-1],
                            # 带上文件夹层级：只写「绑定名 -> 目标根」的话，传一个文件夹
                            # 之后列表里只看得到一堆文件名，用户会以为"那个文件夹没出现"。
                            "remotePath": "%s -> %s%s" % (bname, root,
                                                          ("/" + sub) if sub else ""),
                            "size": f.get("size", 0),
                            "uploaded": f.get("size", 0) if st in SETTLED_STATES else 0,
                            "progress": int(ts.get("progress") or 0),
                            "status": {
                                ST_QUEUED: "pending",
                                ST_UPLOADING: "uploading",
                                ST_FAILED: "failed",
                                ST_MISSING: "pending",
                                ST_MISMATCH: "pending",
                                # 完成后要留在列表里显示「已完成」，不能凭空消失 ——
                                # 本地上传队列的 done 任务是保留的，两边表现必须一致。
                                ST_UPLOADED: "done",
                                ST_DONE: "done",
                            }.get(st, "pending"),
                            "error": ts.get("error", ""),
                            "speed": 0,
                            "finishedAt": done_at if st in SETTLED_STATES else 0,
                        }
                        if row["status"] == "done":
                            finished.append(row)
                        else:
                            result.append(row)
            # 顺序有讲究：前端 renderQueue 会把整个列表 reverse（让最新的显示在最上面）。
            # 所以这里把「已完成」放前面且按完成时间**正序**，reverse 之后它就落到下方，
            # 且最新完成的那条在已完成区的最上面；进行中放后面，reverse 后自然排在最显眼的位置。
            finished.sort(key=lambda r: r["finishedAt"])
            if len(finished) > BINDING_DONE_MAX:
                finished = finished[-BINDING_DONE_MAX:]
            result = finished + result
        return result

    # -- 内部 --

    def _find(self, binding_id):
        for b in self._bindings:
            if b.get("id") == binding_id:
                return b
        return None
