# -*- coding: utf-8 -*-
"""应用 HTTP 接口：统一网关 / 本机调试共用一个 handler。

面向前端的功能面：
* 连接：登录 / 登出 / 连接状态 / 诊断（OpenList 地址 + 账号密码，token 自动续期）
* 浏览：左栏本地目录树（白名单内）、右栏云端目录树
* 上传：把本地文件/文件夹加入持久化队列（进度、自动重试、手动重传、断点续传）
* 对比：双向差异（本地多出 / 云端多出 / 大小不一致），是「上传」与「下载」两个
  方向的共同起点
* 下载：把云端文件/目录加入下载队列（断点续传、覆盖本地、进度同上传）
* 清理：删除多余文件。**这是应用唯一的不可逆操作**，云端走 OpenList
  `fs/remove`，本地走白名单内校验后的删除，每次都在 `audit.log.jsonl` 留痕。

安全边界：
* 本地路径一律走 `accessible_paths()` 白名单，越界直接 400（读与删一视同仁）。
* 凭据（StaticHash、token）只在服务端，接口永不明文回显。
* 网关前缀剥离对大小写与重复斜杠宽容（飞牛网关转发不规整是常态）。
"""

import json
import os
import shutil
import socketserver
import stat as stat_mod
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config
import hasher
import httpclient
import openlist
import upload_queue
import download_queue
import bindings as bindings_mod
import transfer_records
import verify as verify_mod

MAX_BODY_BYTES = 8 * 1024 * 1024        # 接口的 JSON 请求体上限（文件内容走流式，不经过这里）


class ApiError(Exception):
    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# 请求与路径辅助
# --------------------------------------------------------------------------

def _arg(query, key, default=None):
    """取查询参数。`parse_qs` 默认丢弃空值参数，靠 default 兜底。"""
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _query_of(path):
    parsed = urlparse(path)
    return parsed.path, parse_qs(parsed.query, keep_blank_values=True)


def strip_prefix(path, app_name):
    """把网关前缀剥掉。返回剩余的应用内路径。"""
    normalized = path
    while "//" in normalized:
        normalized = normalized.replace("//", "/")

    lowered = normalized.lower()
    for prefix in ("/app/" + app_name.lower(), "/" + app_name.lower()):
        if lowered.startswith(prefix):
            rest = normalized[len(prefix):]
            return rest if rest.startswith("/") else "/" + rest

    marker = lowered.find("/api/")
    if marker >= 0:
        return normalized[marker:]

    if normalized in ("", "/"):
        return "/"
    return normalized


def is_path_allowed(target, roots):
    """本地路径白名单校验（normcase 归一，Windows 盘符大小写不误判）。"""
    if not target:
        return False
    target = os.path.normcase(os.path.abspath(target))
    for root in roots:
        root_norm = os.path.normcase(os.path.abspath(root))
        if target == root_norm or target.startswith(root_norm.rstrip(os.sep) + os.sep):
            return True
    return False


def normalize_cloud(path):
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts)


def join_cloud(*parts):
    segs = []
    for part in parts:
        segs.extend(x for x in str(part).replace("\\", "/").split("/") if x)
    return "/" + "/".join(segs)


def expand_local_dir(local_dir, cloud_dir, excludes, include_base=True):
    """把一个本地目录递归展开成上传项，**顺手带回文件大小**。

    为什么不用 `os.walk` + 事后再 `isfile` / `getsize`：那条路径每个文件要
    3 次 stat（walk 内部一次、isfile 一次、getsize 一次），而 `DirEntry.stat()`
    本身带缓存 —— 一次调用就同时给出「是不是普通文件」和大小。一键上传大目录时
    「点了没反应」很大一部分就耗在这里。

    `include_base`：True（浏览模式）把目录名拼进云端路径（`dst/目录名/子文件`）；
    False（比对模式）直接把内容铺进给定目录（`dst/子文件`）—— 比对结果里给的
    云端路径**已经是这个目录自己的路径**，再补一层目录名就会多出一层。

    返回 `[{"localPath", "remotePath", "size"}, ...]`。
    非普通文件（设备、FIFO 等）直接跳过；**符号链接跟随**（与 os.walk + isfile 一致）。
    """
    items = []
    base = os.path.basename(str(local_dir).rstrip(os.sep)) or ""
    if not include_base:
        base = ""
    excludes = verify_mod.effective_excludes(excludes)
    stack = [(local_dir, "")]
    while stack:
        current, rel = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue        # 读不了的子目录跳过，不拖垮整个上传
        for entry in entries:
            name = entry.name
            if verify_mod.match_exclude(name, excludes):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = (rel + "/" + name) if rel else name
                    stack.append((entry.path, child))
                    continue
                st = os.stat(entry.path)        # 跟随符号链接，取真实大小
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            child = (rel + "/" + name) if rel else name
            items.append({
                "localPath": entry.path,
                "remotePath": join_cloud(cloud_dir, base, child),
                "size": int(st.st_size),
            })
    return items


# 「不限深度」的表示值，与 bindings.UNLIMITED_DEPTH 保持同一套语义。
# verify.scan_* 内部是 max(0, depth)，负数会被悄悄变成 0（只看当前层），
# 所以必须在入口就把 -1 这类值翻译成一个大整数，而不是直接透传下去。
DEPTH_UNLIMITED = 999
DEPTH_MAX = 999


def resolve_depth(raw, fallback=3):
    """宽松版：非法值回退到 fallback。

    写配置时用它 —— 用户填错一个深度不该让整份配置存不进去。
    """
    try:
        depth = int(raw if raw not in (None, "") else fallback)
    except (TypeError, ValueError):
        depth = 3
    if depth < 0:
        return DEPTH_UNLIMITED
    return min(depth, DEPTH_MAX)


def resolve_depth_strict(raw, fallback=3):
    """严格版：非法值直接 400。

    接口显式传参（对比、扫描）时用它 —— 静默回退会掩盖前端的传参 bug。
    """
    if raw in (None, ""):
        return resolve_depth(fallback)
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        raise ApiError("深度必须是整数：%r" % (raw,))
    if depth < 0:
        return DEPTH_UNLIMITED
    return min(depth, DEPTH_MAX)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _str_list(raw, limit=20000):
    """把接口传来的路径清单收敛成去掉空白的字符串列表（保序去重）。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    seen = set()
    for item in raw:
        path = str(item or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _local_roots_norm(roots):
    return set(os.path.normcase(os.path.abspath(r)) for r in roots)


def _copy_pairs(raw, limit=20000):
    """把复制请求里的 `items` 归一成 `[(源, 目标, 附加信息)]`，目标为空串表示"平铺进 target"。

    两种形态：

    * `{"src": 源路径, "dst": 目标路径}` —— **精确落位**。目录比对必须用这种：
      比对结果的相对层级是比对语义的一部分，只给一个"目标目录"的话，服务端
      只能把条目按名字平铺进去，`sub/dir/file` 会变成 `目标根/file`。
    * 字符串 —— 旧形态，配 `target` 用，按名字平铺进 `target`。

    第三项 `extra` 是**可选**的 `{"size", "isDir"}`：前端从比对结果里带过来，
    让「同步完成的复制」（不产生任务的那两条链路）也能在传输列表里显示真实大小
    与"这是目录"。缺字段时它就是个空 dict，行为与旧版一致。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            src = str(item.get("src") or item.get("path") or "").strip()
            dst = str(item.get("dst") or "").strip()
            extra = {"size": item.get("size"), "isDir": bool(item.get("isDir"))}
        else:
            src, dst = str(item or "").strip(), ""
            extra = {"size": None, "isDir": False}
        if not src:
            continue
        out.append((src, dst, extra))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# 比对来源（本地 / 云端）
# --------------------------------------------------------------------------

SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"
_SIDE_LABEL = {"left": "左侧", "right": "右侧"}


def normalize_side(value, default=SOURCE_LOCAL):
    """把来源类型收敛成 local / cloud；认不出来的值回退 default。"""
    side = str(value or "").strip().lower()
    if side in ("cloud", "remote", "openlist", "server"):
        return SOURCE_CLOUD
    if side in ("local", "nas", "fs", "file", "filesystem"):
        return SOURCE_LOCAL
    return default


def resolve_source(body, key, legacy_key, default_side, cfg):
    """解析比对某一侧的来源，返回 `(side, path)`。

    两种请求形状都认：

    * 新：`{"left": {"side": "cloud", "path": "/backup"}}`
    * 旧：`{"localRoot": "/vol1/1000", "cloudRoot": "/backup"}` —— 等价于
      「左 = 本地、右 = 云端」。

    旧形状保留是为了让已有调用方（脚本、旧前端缓存）不必跟着升级；新形状
    才是「两侧各自可以是本地或云端」的表达方式。
    """
    node = body.get(key)
    if isinstance(node, dict):
        side = normalize_side(node.get("side"), default_side)
        path = str(node.get("path") or "").strip()
    else:
        side = default_side
        path = str(body.get(legacy_key) or cfg.get(legacy_key) or "").strip()
    if side == SOURCE_CLOUD:
        path = normalize_cloud(path or "/")
    return side, path


def scan_source(ctx, which, source, depth, excludes, workers):
    """按来源类型扫描一侧，返回 `(map, local_errors)`。

    报错必须说清**是哪一侧、哪种原因**：

    * 只说 `object not found`，用户不知道该改哪一个输入框；
    * 云端那四种情形（没连上 / 不存在 / 不是目录 / 没权限）对用户的下一步动作
      完全不同 —— 一个是去「连接」页填地址，一个是把路径改对，一个是换账号。
      把它们糊成一句"加载失败"等于让人猜。
    """
    side, path = source
    label = _SIDE_LABEL.get(which, which)

    if side == SOURCE_LOCAL:
        if not path:
            raise ApiError("%s：没有指定本地目录" % label)
        roots = config.accessible_paths()
        if not is_path_allowed(path, roots):
            raise ApiError("%s：本地目录不在允许访问的范围内（%s）" % (label, path))
        if not os.path.isdir(path):
            raise ApiError("%s：本地目录不存在或不可读（%s）" % (label, path))
        try:
            return verify_mod.scan_local(path, depth, excludes)
        except (verify_mod.ScanLimitReached, ValueError) as exc:
            raise ApiError("%s：%s" % (label, exc))
        except OSError as exc:
            raise ApiError("%s：读取本地目录失败（%s）：%s" % (label, path, exc))

    client = ctx.client()
    if not client.is_configured():
        raise ApiError("%s：尚未连接 OpenList，请先在「连接」页填写地址与账号"
                       % label, status=401)
    # 根目录不做存在性探测：它是每个存储必然存在的起点，而不同版本的
    # OpenList 对 `fs/get "/"` 的行为并不一致（有的返回根节点，有的报 not found）。
    # 拿一个"可能报错"的请求去判存在性，会把能用的配置判成不能用。
    if path != "/":
        try:
            node = client.stat(path)
        except openlist.AuthError as exc:
            raise ApiError("%s：云端目录无权访问（%s）：%s" % (label, path, exc),
                           status=401)
        except openlist.OpenlistError as exc:
            if getattr(exc, "kind", "") == "network":
                raise ApiError("%s：连接 OpenList 失败（%s）：%s" % (label, path, exc),
                               status=502)
            raise ApiError("%s：无法访问云端目录（%s）：%s" % (label, path, exc))
        if node is None:
            raise ApiError("%s：云端目录不存在（%s）" % (label, path))
        if not node.get("is_dir"):
            raise ApiError("%s：云端路径不是目录，无法比对（%s）" % (label, path))

    try:
        return verify_mod.scan_cloud(client, path, depth, excludes,
                                     concurrency=workers), []
    except verify_mod.ScanLimitReached as exc:
        raise ApiError("%s：%s" % (label, exc))
    except RuntimeError as exc:
        raise ApiError("%s：扫描云端目录失败（%s）：%s" % (label, path, exc))


def _task_ids(data):
    """从 OpenList 复制响应里取出后台任务 id（同存储复制是同步的，没有任务）。"""
    out = []
    for item in ((data or {}).get("tasks") or []):
        if isinstance(item, dict) and item.get("id"):
            out.append(item["id"])
    return out


def _size_of(extra):
    """从 `_copy_pairs` 的附加信息里取大小；拿不到就给 0（前端显示「—」）。"""
    try:
        return max(0, int((extra or {}).get("size") or 0))
    except (TypeError, ValueError):
        return 0


def _local_record(src, dst, extra, error="", is_dir=None):
    """构造一条**本地复制**传输记录。

    目录按一条记，`size` 记 0，`isDir=True` —— 前端据此显示「目录」而不是
    「0 B / 0 B」。文件的大小优先信调用方给的（比对结果里带过来的），
    没有就现取（复制已经成功了，这一次 stat 不会失败到影响结果）。
    """
    dir_flag = bool(os.path.isdir(src) and not os.path.islink(src)) \
        if is_dir is None else bool(is_dir)
    size = 0
    if not dir_flag:
        size = _size_of(extra)
        if not size:
            try:
                size = int(os.path.getsize(dst))
            except OSError:
                size = 0
    return {
        "kind": transfer_records.KIND_LOCAL,
        "name": os.path.basename(str(src).rstrip(os.sep)) or str(src),
        "srcPath": str(src),
        "dstPath": str(dst),
        "size": size,
        "isDir": dir_flag,
        "status": (transfer_records.FAILED if error else transfer_records.DONE),
        "error": error or "",
    }


# 传输列表下发的 OpenList 任务 id 前缀（与 openlist 任务表里的裸 tid 区分开）
OL_TASK_ID_PREFIX = "ol_"

# 传输列表**当前真正会展示**的 OpenList 任务类型（见 h_openlist_tasks，只聚合 copy）。
# 「全部取消」「全部重试」一律按这个常量来，绝不按 openlist.TASK_TYPES 全量——
# 用户屏幕上根本看不到的类型，不该被一次「全部取消」停掉：那不是"取消全部"，
# 那是"顺手把别人/别的应用的任务也停了"，而用户连它们的存在都不知道。
#
# ⚠️ 这一项必须与 h_openlist_tasks 的聚合范围**同步改**：等 R05 把 upload/move
# 聚合进列表（前端加 taskType 标签）时，这里才跟着扩成 ("copy", "upload", ...)。
# 两步必须一起改，只改一边等于让"所见即所取消"再次失效。
VISIBLE_TASK_TYPES = ("copy",)


def split_openlist_task_id(raw, task_type=None):
    """把前端给的 OpenList 任务 id 拆成 `(tid, type)`。

    前端沿用 `ol_<tid>` 约定（与 `/api/openlist/tasks` 下发的 id 完全一致），
    也接受裸 tid（手动排障时方便）。type 只认 `copy` / `upload`，认不出来就返回
    None —— 由调用方逐个类型试，前端不必关心任务类型。

    这里刻意用 `openlist.TASK_TYPES`（**不是** VISIBLE_TASK_TYPES）：单条接口
    面对的是"用户拿着一个 id 让你对这一个任务动手"，多试几个类型只会更准，
    而且将来列表扩类型时前端不用跟着改。
    """
    task_id = str(raw or "").strip()
    if task_id.startswith(OL_TASK_ID_PREFIX):
        task_id = task_id[len(OL_TASK_ID_PREFIX):]
    kind = str(task_type or "").strip().lower()
    if kind not in openlist.TASK_TYPES:
        kind = None
    return task_id, kind


def _task_buckets(client, task_type):
    """拉一次某类任务，返回 `(undone tid 列表, done 的 {tid: state}, 是否成功)`。

    回读核验**必须同时看 undone 和 done**：任务离开 undone 有两种可能 —— 被取消
    （state=4），或者在这几秒里自己跑完了（state=2 成功 / state=7 失败）。
    旧口径 `cancelled = requested - len(undone)` 会把后者也算成"已取消"，
    于是"点了全部取消，正好有任务跑完"时会报出假的取消数。这里把两种结局分开。
    """
    data = client.list_tasks(task_type)
    if data is None:
        return [], {}, False
    ids, done = [], {}
    for task in data.get("undone") or []:
        if isinstance(task, dict) and task.get("id"):
            ids.append(str(task["id"]))
    for task in data.get("done") or []:
        if isinstance(task, dict) and task.get("id"):
            try:
                done[str(task["id"])] = int(task.get("state") or 0)
            except (TypeError, ValueError):
                done[str(task["id"])] = 0
    return ids, done, True


def _cancel_openlist_undone(client, log=None):
    """取消传输列表**可见类型**（`VISIBLE_TASK_TYPES`）的全部未完成任务，并回读核验。

    返回报告 dict：

    ```
    {"requested", "cancelled", "finished", "remaining", "byType": {...}, "error"}
    ```

    流程（每一步都不能省，省了就退回"点了没反应/谎报成功"）：

    1. 拉 undone 拿 tid（只取 `VISIBLE_TASK_TYPES`，即用户屏幕上看得见的那些）；
    2. 批量取消，解析 per-tid 结果；
    3. **回读 undone + done** —— 离开 undone 不等于"被取消"，还要看 done 里的
       state 是不是 4(canceled)；state 2/7 是这期间自己跑完的，单列 `finished`；
    4. 对残留逐条兜底，再回读一次，剩余的条数如实返回。

    `cancelled` 只数「回读后 done 里 state=4 的 tid」，不是"请求发出去几条"，
    也不是"不在 undone 里的条数"。
    """
    log = log or (lambda *a, **k: None)
    report = {"requested": 0, "cancelled": 0, "finished": 0, "remaining": 0,
              "byType": {}, "error": ""}
    for task_type in VISIBLE_TASK_TYPES:
        detail = {"requested": 0, "cancelled": 0, "finished": 0,
                  "remaining": 0, "failed": [], "error": ""}
        tids, _, ok = _task_buckets(client, task_type)
        detail["requested"] = len(tids)
        if not ok:
            detail["error"] = "查询任务列表失败"
            report["byType"][task_type] = detail
            continue
        if not tids:
            report["byType"][task_type] = detail
            continue

        batch = client.cancel_tasks(tids, task_type)
        detail["failed"] = list(batch.get("failed") or [])

        residual, done_after, after_ok = _task_buckets(client, task_type)
        if not after_ok:
            # 拿不到回读结果 → 只能给批量接口的结论，并明确标「未核实」
            detail["cancelled"] = int(batch.get("ok") or 0)
            detail["remaining"] = max(0, len(tids) - int(batch.get("ok") or 0))
            detail["error"] = "取消后无法回读任务列表，残留数未核实"
            report["byType"][task_type] = detail
            continue

        if residual:
            # 批量接口没给逐条结果、或对某些 tid 报错 → 逐条再取消一次。
            # 单条接口的返回语义清楚得多，是最后一道保险。
            for tid in residual:
                if client.cancel_task(tid, task_type):
                    continue
                detail["failed"].append({"id": tid, "error": "逐条取消失败"})
            final, final_done, final_ok = _task_buckets(client, task_type)
            if final_ok:
                residual = final
                done_after = final_done

        detail["remaining"] = len(residual)
        # 只认 done 里明确标成 canceled(4) 的才算"被我们停下"；
        # 2(succeeded)/7(failed) 是这期间自己结束的，不能记到取消头上。
        for tid in tids:
            if tid in residual:
                continue
            state = done_after.get(tid, -1)
            if state == 4:
                detail["cancelled"] += 1
            elif state in (2, 7):
                detail["finished"] += 1
        if detail["remaining"]:
            log("OpenList %s 任务取消后仍有 %d 个未停下"
                % (task_type, detail["remaining"]))
        report["byType"][task_type] = detail
        report["requested"] += detail["requested"]
        report["cancelled"] += detail["cancelled"]
        report["finished"] += detail["finished"]
        report["remaining"] += detail["remaining"]
        if detail.get("error") and not report["error"]:
            report["error"] = detail["error"]
    return report


# 可以通过接口修改的配置项及其约束。
# 之前这里是完全开放的：并发数能填 999（真起 999 个线程）、
# scanDepth 能填 "abc"（写进配置后每次读取都崩）、
# excludeNames 能填成字符串（按字符逐个当排除名匹配）。
_UPLOAD_CONCURRENCY_RANGE = (1, 8)     # 与设置页 #cfgConc 的 max 一致
_SCAN_CONCURRENCY_RANGE = (1, 32)      # 与 verify.SCAN_CONCURRENCY_MAX 一致
_MAX_ATTEMPTS_RANGE = (0, 10)
_AUTO_SCAN_INTERVAL_MIN = 1  # 全量扫描间隔下限（天）
_LOCAL_SOURCE_MODES = ("fs", "cloud")

_BOOL_FIELDS = ("skipExisting", "verifyAfterUpload", "autoScan")
_STR_FIELDS = ("localRoot", "cloudRoot", "localSourceRoot", "scanScheduleTime")


def sanitize_config(merged):
    """把配置收敛到合法范围，越界就夹到边界值（不报错，避免用户填错整份配置存不进去）。"""
    lo, hi = _UPLOAD_CONCURRENCY_RANGE
    try:
        merged["uploadConcurrency"] = min(hi, max(lo, int(merged.get("uploadConcurrency"))))
    except (TypeError, ValueError):
        merged["uploadConcurrency"] = config.DEFAULT_CONFIG["uploadConcurrency"]

    lo, hi = _MAX_ATTEMPTS_RANGE
    try:
        merged["maxAttempts"] = min(hi, max(lo, int(merged.get("maxAttempts"))))
    except (TypeError, ValueError):
        merged["maxAttempts"] = config.DEFAULT_CONFIG["maxAttempts"]

    merged["scanDepth"] = resolve_depth(merged.get("scanDepth"),
                                        config.DEFAULT_CONFIG["scanDepth"])

    lo, hi = _SCAN_CONCURRENCY_RANGE
    try:
        merged["scanConcurrency"] = min(hi, max(lo, int(merged.get("scanConcurrency"))))
    except (TypeError, ValueError):
        merged["scanConcurrency"] = config.DEFAULT_CONFIG["scanConcurrency"]

    try:
        days = int(merged.get("fullScanIntervalDays"))
    except (TypeError, ValueError):
        days = config.DEFAULT_CONFIG["fullScanIntervalDays"]
    merged["fullScanIntervalDays"] = max(_AUTO_SCAN_INTERVAL_MIN, min(30, days))

    # 全局绑定扫描并发数（1-10）
    try:
        merged["bindingScanConcurrency"] = min(10, max(1, int(merged.get("bindingScanConcurrency"))))
    except (TypeError, ValueError):
        merged["bindingScanConcurrency"] = config.DEFAULT_CONFIG["bindingScanConcurrency"]

    # 扫描时间验证（HH:MM 格式）
    sched = str(merged.get("scanScheduleTime") or "03:00")
    try:
        h, m = [int(x) for x in sched.split(":")[:2]]
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        merged["scanScheduleTime"] = "%02d:%02d" % (h, m)
    except Exception:
        merged["scanScheduleTime"] = "03:00"

    for key in _BOOL_FIELDS:
        merged[key] = _as_bool(merged.get(key))

    for key in _STR_FIELDS:
        merged[key] = str(merged.get(key) or "")

    raw_excludes = merged.get("excludeNames")
    if isinstance(raw_excludes, str):
        raw_excludes = [s for s in raw_excludes.split(",")]
    if not isinstance(raw_excludes, (list, tuple)):
        raw_excludes = config.DEFAULT_CONFIG["excludeNames"]
    merged["excludeNames"] = [str(s).strip() for s in raw_excludes if str(s).strip()]

    if merged.get("localSourceMode") not in _LOCAL_SOURCE_MODES:
        merged["localSourceMode"] = config.DEFAULT_CONFIG["localSourceMode"]
    return merged


# --------------------------------------------------------------------------
# 应用上下文
# --------------------------------------------------------------------------

class AppContext(object):
    def __init__(self, app_name):
        self.app_name = app_name
        self.log = lambda *a, **k: None
        # 网关 socket 探活发现「已有实例在跑」时置 True，main 据此直接退出
        self.single_instance_conflict = False
        self._store = None
        self._client = None
        self._queue = None
        self._downloads = None
        self._bindings = None
        self._records = None
        # 惰性单例的初始化锁。HTTP 服务是多线程的，首屏会并发打
        # /api/status、/api/config、/api/bindings 等，不加锁会造出
        # 两份 UploadQueue / BindingStore，同一批任务并发执行、互相覆盖落盘。
        # 必须用 RLock 而不是 Lock：bindings() 在锁内会调用 queue()/client()，
        # 它们也会获取同一把锁，普通 Lock 会死锁。
        self._init_lock = threading.RLock()

    def cfg(self):
        return config.load_config()

    def store(self):
        if self._store is None:
            with self._init_lock:
                if self._store is None:
                    self._store = openlist.CredentialStore()
        return self._store

    def client(self):
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    self._client = openlist.OpenlistClient(self.store(), log=self.log)
        return self._client

    def queue(self):
        """惰性创建并启动上传队列。"""
        if self._queue is None:
            with self._init_lock:
                if self._queue is None:
                    cfg = self.cfg()
                    self._queue = upload_queue.UploadQueue(
                        self.client(), log=self.log,
                        max_attempts=int(cfg.get("maxAttempts") or 3))
                    self._queue.start(workers=int(cfg.get("uploadConcurrency") or 2))
        return self._queue

    def downloads(self):
        """惰性创建并启动下载队列（与上传队列**独立**）。"""
        if self._downloads is None:
            with self._init_lock:
                if self._downloads is None:
                    cfg = self.cfg()
                    self._downloads = download_queue.DownloadQueue(
                        self.client(), log=self.log,
                        max_attempts=int(cfg.get("maxAttempts") or 3))
                    self._downloads.start(workers=int(cfg.get("uploadConcurrency") or 2))
        return self._downloads

    def transfer_queues(self):
        """上传 + 下载两个队列。

        「传输列表」把两者合成一屏，所以重试 / 取消 / 删除这类按 id 操作的
        接口必须先判断 id 属于哪一个 —— 打错队列只会得到"任务不存在"。
        """
        return (self.queue(), self.downloads())

    def records(self):
        """传输记录（本地复制 / 同存储云端复制的回执）。

        这两条链路不产生队列任务也不产生 OpenList 任务，不单独记录的话
        传输列表里就什么都看不到（BUG-3）。
        """
        if self._records is None:
            with self._init_lock:
                if self._records is None:
                    self._records = transfer_records.TransferRecordStore(log=self.log)
        return self._records

    def find_transfer(self, task_id):
        """按任务 id 找到它所属的队列。返回 (queue, task) 或 (None, None)。"""
        if not task_id:
            return None, None
        for queue in self.transfer_queues():
            task = queue.get(task_id)
            if task:
                return queue, task
        return None, None

    def audit(self, event, **fields):
        """把破坏性操作写进审计日志（append-only）。

        「清除多余文件」是应用的**唯一**不可逆操作。留一条本地记录，用户事后
        至少能确认「什么时候、删了哪些路径」，而不是只剩一句"已删除"。
        """
        payload = {"at": int(time.time()), "event": event}
        payload.update(fields)
        try:
            with open(os.path.join(config.data_dir(), "audit.log.jsonl"),
                      "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
                fh.flush()
        except Exception as exc:
            self.log("[audit] 写审计日志失败：%r" % (exc,))

    def bindings(self):
        """惰性创建绑定存储。"""
        if self._bindings is None:
            with self._init_lock:
                if self._bindings is None:
                    self._bindings = bindings_mod.BindingStore(
                        self.client(), queue=self.queue(), log=self.log)
                    # 启动时按已保存的配置恢复自动扫描
                    try:
                        cfg = self.cfg()
                        self._bindings.configure_auto_scan(
                            bool(cfg.get("autoScan")),
                            days=cfg.get("fullScanIntervalDays", 7),
                            schedule_time=cfg.get("scanScheduleTime", "03:00"),
                        )
                        self._bindings.set_scan_concurrency(
                            cfg.get("bindingScanConcurrency", 2)
                        )
                    except Exception:
                        pass
        else:
            # queue 可能在 reset_queue 后被重建，保持引用最新
            self._bindings.queue = self._queue
        return self._bindings

    def reset_queue(self):
        """配置变更后重建队列（并发数 / 重试次数才会生效）。

        重建前记下「是否暂停」，重建后原样恢复 —— 否则用户暂停了队列、
        再去改一下并发数，队列会自己跑起来。

        顺序很关键：**先停完再置空**。早先是「锁内先置空、锁外再 stop」，
        于是 stop 里的 join 超时（worker 卡在长请求）时，旧 worker 还活着，
        新队列已经拿着同一份 queue.json 起来了 —— 两个队列各有一份任务列表，
        谁最后 save 谁覆盖对方。现在只要有一个 worker 没退出就**报错拒绝重建**，
        旧队列原样保留（认领表也不动）。
        """
        # stop() 内部要 join worker，若在 _init_lock 内调用会挡住所有请求，
        # 所以锁内只取引用，stop() 放到锁外执行。
        old_queue = None
        old_downloads = None
        was_paused = False
        with self._init_lock:
            if self._queue is not None:
                try:
                    was_paused = self._queue.is_paused()
                except Exception:
                    was_paused = False
                old_queue = self._queue
            if self._downloads is not None:
                old_downloads = self._downloads

        # 锁外 stop：join worker 不阻塞其他请求
        for label, old in (("上传", old_queue), ("下载", old_downloads)):
            if old is None:
                continue
            try:
                stopped = old.stop()
            except Exception as exc:
                self.log("重建队列时停止%s队列失败：%s" % (label, exc))
                stopped = True
            if stopped is False:
                # 老队列没能全停：撤回归档，让它继续干活。
                # 不撤的话 self._queue 指向一个"已收到停止信号、worker 正在退出"的
                # 队列，之后新任务能入队却永远没人处理（表现是"上传一直排队不动"）。
                # 直接 start() 会 clear 掉 _stop，并把还在跑的 worker 也纳入同一份
                # 任务列表（同一个对象、同一把锁，不存在两个队列各写一份文件的问题）。
                try:
                    old._workers = []
                    old.start(workers=1)
                except Exception:
                    pass
                raise ApiError("%s队列仍有任务没停下来，改并发数需要它们先退出，"
                               "请稍后重试" % label)

        with self._init_lock:
            self._queue = None
            self._downloads = None
        # bindings 持有旧 queue 引用，下次 bindings() 时会重新关联
        if was_paused:
            try:
                self.queue().set_paused(True)
                self.downloads().set_paused(True)
            except Exception:
                pass


def _tail_lines(path, n, block=64 * 1024):
    """从文件尾部读最近 n 行（不把整份日志读进内存）。

    `readlines()[-n:]` 是整文件读入再切片：NAS 上 info.log 长期不轮转会到
    几十 MB，而前端日志面板每 3 秒拉一次 —— 峰值内存与磁盘读都被放大成
    「页面开着的时候一直读整份文件」。这里从尾部按块回退，够 n 行就停。
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return []                      # 日志还没生成 / 刚被轮转走，当空处理
    with fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size <= 0:
            return []
        chunk = max(block, n * 160)
        lines = []
        while True:
            start = max(0, size - chunk)
            fh.seek(start)
            data = fh.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]      # 块首那个半行不要
            if lines and lines[-1] == b"":
                lines.pop()            # 文件以 \n 结尾时 split 会多一个空串
            if start == 0 or len(lines) >= n:
                break
            chunk *= 2
    return [ln.decode("utf-8", "replace").rstrip("\r") for ln in lines[-n:]]


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DoubaoDrive/" + config.APP_VERSION

    @property
    def ctx(self):
        return self.server.ctx

    def log_message(self, fmt, *args):
        try:
            # 过滤高频轮询的 GET 200 成功响应，避免刷屏：
            # 前端为了实时更新状态，会每几秒轮询 /api/bindings、/api/openlist/tasks、
            # /api/upload/list、/api/log 等接口，全是 200 成功，记录下来没有调试价值。
            # 只保留：POST/DELETE 等写操作、GET 的非 200 响应（错误）。
            # args 格式：(requestline, status_code, size)
            # requestline 如 "GET /app/cloud-sync/api/bindings HTTP/1.1"
            if len(args) >= 2:
                requestline = str(args[0])
                status = str(args[1])
                # GET 请求且状态码 2xx → 过滤
                if requestline.startswith("GET ") and status.startswith("2"):
                    return
            self.ctx.log("[http] " + (fmt % args))
        except Exception:
            pass

    # -- 响应基础 --

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"ok": False, "error": str(message)}, status)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            # 没 Content-Length 却有 chunked：http.server 不会解 chunked，
            # 当成 {} 继续执行等于"静默把请求体丢掉"，接口会拿空参数往下跑
            # （比如取消任务时报"任务不存在"而不是明确拒绝）。
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise ApiError("不支持 chunked 请求体，请带 Content-Length", 411)
            return {}
        if length > MAX_BODY_BYTES:
            # 拒绝超大请求体时必须排空并关连接，否则 keep-alive 会把残留字节
            # 当成下一个请求解析，客户端收到两个粘在一起的响应。
            self.close_connection = True
            self.send_response(413)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError("请求体顶层必须是对象")
        return data

    # -- 路由 --

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        route_path, query = _query_of(self.path)
        route = strip_prefix(route_path, self.ctx.app_name)

        if method in ("GET", "HEAD") and self._serve_static(route):
            return

        handler = self._resolve_handler(method, route)
        if handler is None:
            # 未知路由也必须先把请求体读掉。POST 带 body 时直接回 404 会让残留
            # 字节留在连接里，keep-alive 的下一行请求被拼上这段 JSON ——
            # 实测同一条连接上后续请求变成 400 Bad request syntax，
            # 用户看到的是"点了没反应 / 整页接口全挂"（多见于前端缓存了旧版页面，
            # 还在调已下线的接口）。
            if method == "POST":
                try:
                    drained = self._read_body()
                except ApiError as exc:
                    self._error(exc.message, exc.status)
                    return
                if drained is None:      # 413 已响应
                    return
            self._error("接口不存在：%s %s" % (method, route), 404)
            return

        try:
            body = self._read_body() if method == "POST" else {}
            if body is None:      # 413 已响应
                return
            handler(query, body)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except openlist.AuthError as exc:
            self._error("登录态失效：%s" % exc, 401)
        except openlist.NetworkError as exc:
            self._error("连不上 OpenList：%s" % exc, 502)
        except openlist.OpenlistError as exc:
            self._error(str(exc), 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self.ctx.log("[api] 未处理异常 %s %s: %r" % (method, self.path, exc))
            except Exception:
                pass
            self._error("服务器内部错误：%s" % exc, 500)

    def _resolve_handler(self, method, route):
        lookup = "GET" if method == "HEAD" else method
        name = _ROUTES.get((lookup, route))
        if name:
            return getattr(self, name)
        return None

    # -- 静态文件 --

    def _serve_static(self, route):
        if route in ("/", ""):
            route = "/index.html"
        if not route.startswith("/api/"):
            rel = route.lstrip("/")
            if ".." in rel.split("/"):
                return False
            target = os.path.join(config.www_dir(), *rel.split("/"))
            if os.path.isfile(target):
                content_type = "text/html; charset=utf-8"
                if rel.endswith(".js"):
                    content_type = "application/javascript; charset=utf-8"
                elif rel.endswith(".css"):
                    content_type = "text/css; charset=utf-8"
                elif rel.endswith(".png"):
                    content_type = "image/png"
                elif rel.endswith(".svg"):
                    content_type = "image/svg+xml"
                elif rel.endswith(".ico"):
                    content_type = "image/x-icon"
                try:
                    with open(target, "rb") as fh:
                        body = fh.read()
                except OSError:
                    return False
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return True
        return False

    # ==================== 接口实现 ====================

    # -- 信息 / 状态 --

    def h_info(self, query, body):
        backend, note = _transport_note()
        self._json({
            "ok": True,
            "app": self.ctx.app_name,
            "version": config.APP_VERSION,
            "dataDir": config.data_dir(),
            "insideTrim": config.is_inside_trim(),
            "transport": backend,
            "transportNote": note,
            "hashBackend": hasher.backend(),
        })

    def h_status(self, query, body):
        self._json({
            "ok": True,
            "connection": self._connection_view(),
            "queue": self.ctx.queue().counts() if self.ctx._queue else {},
            "config": self._public_config(self.ctx.cfg()),
        })

    # 允许回给前端的配置字段。凭据本来就不在 config.json（在 credentials.json），
    # 但白名单能防止以后往配置里塞敏感字段时被顺手一起吐出去。
    PUBLIC_CONFIG_KEYS = frozenset([
        "localRoot", "cloudRoot", "scanDepth", "scanConcurrency", "excludeNames",
        "uploadConcurrency", "skipExisting", "verifyAfterUpload", "maxAttempts",
        "autoScan", "fullScanIntervalDays", "scanScheduleTime", "bindingScanConcurrency",
        "localSourceMode", "localSourceRoot", "openlistUrl",
    ])

    def _public_config(self, cfg):
        return {k: v for k, v in (cfg or {}).items()
                if k in self.PUBLIC_CONFIG_KEYS}

    def _connection_view(self):
        st = self.ctx.store().status()
        if st.get("configured"):
            try:
                me = self.ctx.client().me() or {}
                st["user"] = {
                    "username": me.get("username", ""),
                    "basePath": me.get("base_path", ""),
                    "permission": me.get("permission", 0),
                    # AList/OpenList 权限位：bit2=write(4) 才是上传权限；
                    # bit3(8) 是 admin，普通上传用户不该依赖它。
                    "canUpload": bool((me.get("permission") or 0) & 0b0100),
                }
                st["online"] = True
            except openlist.OpenlistError as exc:
                st["online"] = False
                st["error"] = str(exc)
            except Exception as exc:
                st["online"] = False
                st["error"] = "异常：%s" % exc
        return st

    # -- 配置 --

    def h_config_get(self, query, body):
        self._json({"ok": True, "config": self._public_config(self.ctx.cfg())})

    def h_config_set(self, query, body):
        incoming = body.get("config") if isinstance(body.get("config"), dict) else body
        if not isinstance(incoming, dict):
            raise ApiError("请求体必须是配置对象")
        cfg = self.ctx.cfg()
        old_concurrency = cfg.get("uploadConcurrency")
        old_max_attempts = cfg.get("maxAttempts")
        merged = config._deep_merge(cfg, incoming)
        sanitize_config(merged)
        # 本地白名单不允许通过接口改（那是环境变量管的）
        config.save_config(merged)
        if merged.get("uploadConcurrency") != old_concurrency \
                or merged.get("maxAttempts") != old_max_attempts:
            self.ctx.reset_queue()
            self.ctx.queue()      # 立即按新配置重启
        # 自动扫描开关/间隔变化：立即生效
        try:
            self.ctx.bindings().configure_auto_scan(
                bool(merged.get("autoScan")),
                days=merged.get("fullScanIntervalDays", 7),
                schedule_time=merged.get("scanScheduleTime", "03:00"),
            )
        except Exception:
            pass
        # 全局绑定扫描并发数变化：立即生效
        try:
            self.ctx.bindings().set_scan_concurrency(
                merged.get("bindingScanConcurrency", 2)
            )
        except Exception:
            pass
        self._json({"ok": True, "config": self._public_config(merged)})

    # -- 连接（OpenList 登录） --

    def h_connect(self, query, body):
        base = body.get("baseUrl") or ""
        username = body.get("username") or ""
        password = body.get("password")
        otp = body.get("otpCode") or ""
        if password is None:
            raise ApiError("缺少密码")
        self.ctx.client().login(base, username, password, otp)
        # 登录成功后确保队列已按配置启动
        self.ctx.queue()
        self._json({"ok": True, "connection": self._connection_view()})

    def h_disconnect(self, query, body):
        self.ctx.client().logout()
        self._json({"ok": True, "connection": self._connection_view()})

    def h_connection(self, query, body):
        self._json({"ok": True, "connection": self._connection_view()})

    def h_connect_test(self, query, body):
        result = self.ctx.client().diagnose()
        self._json({"ok": result.get("ok", False), "diagnose": result})

    # -- 本地浏览 --

    def h_local_roots(self, query, body):
        self._json({"ok": True, "roots": config.accessible_paths()})

    def h_local_list(self, query, body):
        path = _arg(query, "path", "") or ""
        aggregate = _arg(query, "aggregate", "") == "1"
        roots = config.accessible_paths()

        if aggregate or not path:
            return self._json({"ok": True, "aggregate": True, "path": "",
                               "entries": _aggregate_roots(roots)})

        if not is_path_allowed(path, roots):
            raise ApiError("路径不在允许访问的范围内：%s" % path)
        if not os.path.isdir(path):
            raise ApiError("目录不存在或不可读：%s" % path)

        entries = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    size = 0
                    mtime = 0
                    if not is_dir:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                            mtime = int(st.st_mtime)
                        except OSError:
                            pass
                    entries.append({"name": entry.name, "path": entry.path,
                                    "isDir": is_dir, "size": size, "mtime": mtime})
        except OSError as exc:
            raise ApiError("读取目录失败：%s" % exc)
        entries.sort(key=lambda e: (not e["isDir"], e["name"].lower()))
        # 找到当前路径所属的存储空间根目录（用于前端面包屑去掉 /vol*/1000 前缀）
        matched_root = ""
        norm_path = os.path.normcase(os.path.abspath(path))
        for root in roots:
            user_dir = os.path.join(root, "1000")
            base = user_dir if os.path.isdir(user_dir) else root
            norm_base = os.path.normcase(os.path.abspath(base))
            if norm_path == norm_base or norm_path.startswith(norm_base + os.sep):
                matched_root = base
                break
        self._json({"ok": True, "aggregate": False, "path": path, "root": matched_root, "entries": entries})

    # -- 云端浏览 --

    def h_cloud_list(self, query, body):
        path = normalize_cloud(_arg(query, "path", "/") or "/")
        client = self.ctx.client()
        entries = []
        # 一次 list_all 就顺带拿回 write：之前为了拿 write 又单独发了一次 fs/list，
        # 翻页的大目录等于白跑两遍。
        meta = {}
        entries_raw = client.list_all(path, meta=meta)
        write = bool(meta.get("write"))
        for node in entries_raw:
            name = node.get("name") or ""
            if not name:
                continue
            entries.append({
                "name": name,
                "isDir": bool(node.get("is_dir")),
                "size": int(node.get("size") or 0),
                "modified": node.get("modified", ""),
                "path": join_cloud(path, name),
            })
        entries.sort(key=lambda e: (not e["isDir"], e["name"].lower()))
        self._json({"ok": True, "path": path, "write": write, "entries": entries})

    def h_cloud_mkdir(self, query, body):
        path = (body.get("path") or "").strip()
        if not path:
            raise ApiError("缺少 path")
        self.ctx.client().ensure_dir(normalize_cloud(path))
        self._json({"ok": True})

    # -- 对比 --

    def h_cloud_copy(self, query, body):
        """OpenList 服务端复制：srcDir 下的 names 复制到 dstDir。

        跨存储时服务端会建后台任务，同存储时**同步完成**（响应里没有 tasks）。
        两种情况的回执都要给：`tasks` 交给传输列表轮询，同步完成的那些落一条
        `olc_` 传输记录 —— 否则用户点了复制，传输列表里空空如也。
        """
        src_dir = normalize_cloud((body.get("srcDir") or "").strip())
        dst_dir = normalize_cloud((body.get("dstDir") or "").strip())
        names = body.get("names") or []
        if not src_dir or not dst_dir or not names:
            raise ApiError("缺少 srcDir / dstDir / names")
        if isinstance(names, str):
            names = [names]
        names = [str(n) for n in names if str(n).strip()]
        if not names:
            raise ApiError("names 为空")
        data = self.ctx.client().copy(src_dir, dst_dir, names)
        tasks = _task_ids(data)
        if not tasks:
            self._record_cloud_copies([(src_dir, dst_dir, name, None, False)
                                       for name in names])
        self._json({"ok": True, "copied": len(names), "tasks": tasks,
                    "failed": []})

    def _record_cloud_copies(self, entries):
        """给「同步完成的云端复制」落传输记录。

        `entries` 每项是 `(src_dir, dst_dir, name, size, isDir)`。
        只在 OpenList **没有**建后台任务时调用 —— 有任务的复制由 OpenList 自己
        在传输列表里报告状态，再记一份就重复了。
        """
        records = []
        for src_dir, dst_dir, name, size, is_dir in entries:
            src = join_cloud(src_dir, name)
            records.append({
                "kind": transfer_records.KIND_CLOUD,
                "name": name,
                "srcPath": src,
                "dstPath": join_cloud(dst_dir, name),
                "size": size,
                "isDir": bool(is_dir),
                "status": transfer_records.DONE,
            })
        if records:
            self.ctx.records().add_many(records)
            self.ctx.log("已记录 %d 项云端同步复制（无后台任务）" % len(records))
        return records

    def h_compare(self, query, body):
        """双向比对：**两侧各自可以是本地或云端**。

        字段沿用历史的左右约定：`localExtra` 里装的是「仅左侧有」的条目，
        `cloudExtra` 里装的是「仅右侧有」的。侧别由 `left` / `right` 显式声明，
        不再由栏位（左栏=本地）推断 —— 那正是"云端↔云端"以前做不了的原因。
        """
        cfg = self.ctx.cfg()
        depth = resolve_depth_strict(body.get("depth"), cfg.get("scanDepth") or 3)
        scan_workers = verify_mod.normalize_concurrency(cfg.get("scanConcurrency"))
        excludes = cfg.get("excludeNames")

        left = resolve_source(body, "left", "localRoot", SOURCE_LOCAL, cfg)
        right = resolve_source(body, "right", "cloudRoot", SOURCE_CLOUD, cfg)

        left_map, left_errors = scan_source(self.ctx, "left", left, depth,
                                            excludes, scan_workers)
        right_map = scan_source(self.ctx, "right", right, depth,
                                excludes, scan_workers)[0]

        result = verify_mod.compare(left_map, right_map)
        self._json({
            "ok": True,
            "left": {"side": left[0], "path": left[1]},
            "right": {"side": right[0], "path": right[1]},
            # 兼容旧字段：升级期间仍按"本地侧 / 云端侧"给出各自路径，
            # 某一侧不是该类型时给空串 / 根。
            "localRoot": left[1] if left[0] == SOURCE_LOCAL else
                         (right[1] if right[0] == SOURCE_LOCAL else ""),
            "cloudRoot": left[1] if left[0] == SOURCE_CLOUD else
                         (right[1] if right[0] == SOURCE_CLOUD else "/"),
            "depth": depth,
            "result": result,
            "localErrors": left_errors,
        })

    # -- 跨侧复制（云端↔云端、本地↔本地）--

    def h_sync_copy(self, query, body):
        """把 `items` 复制到**各自的目标位置**。

        `items` 每项是 `{"src": 源路径, "dst": 目标路径}`（精确落位）或一个字符串
        （旧形态，按名字平铺进 `target`）。只覆盖**不经上传/下载队列**的两种组合：

        * `side="cloud"` —— 云端 → 云端，走 OpenList 服务端复制（同存储同步完成，
          跨存储由服务端建后台任务，任务 id 一并回传）；
        * `side="local"` —— 本地 → 本地，走本地文件复制。

        **为什么要有 `dst`**：目录比对里「差异文件在哪一层」是语义的一部分。
        只给一个目标目录时，服务端只能拿 `basename` 往目标根目录里塞 ——
        比对结果里 `sub/dir/f.bin` 会落到 `目标根/f.bin`，子目录被压平，
        用户看到的就是「复制到了对比根目录，不是原路径目录」。

        「本地 ↔ 云端」两个方向**刻意不在这里**：它们有断点续传、大小校验、
        失败重试与进度反馈，走上传 / 下载队列。同一件事有两套实现，迟早会漂移。
        """
        side = normalize_side(body.get("side"), "")
        if side not in (SOURCE_LOCAL, SOURCE_CLOUD):
            raise ApiError("缺少 side（cloud 或 local）")
        target = str(body.get("target") or "").strip()
        pairs = _copy_pairs(body.get("items"))
        if not pairs:
            raise ApiError("没有指定要复制的内容")
        # 只有"没带落点"的条目才需要 target；全都带了 dst 时它就是个多余参数。
        if not target and any(not dst for _, dst, _ in pairs):
            raise ApiError("缺少 target（目标目录）")

        if side == SOURCE_CLOUD:
            return self._copy_cloud(pairs, target)
        return self._copy_local(pairs, target)

    def _copy_cloud(self, pairs, target):
        """云端 → 云端：按 `(源目录, 目标目录)` 分组，走 OpenList 批量复制。

        分组键里必须**同时**有目标目录：落点不再统一是 `target`，每个条目可以
        落到不同的子目录（比对结果的相对层级不同）。只用源目录当键，会把发往
        不同目标目录的条目塞进同一次请求 —— 那正是"层级被压平"的另一副面孔。
        """
        target_dir = normalize_cloud(target) if target else ""
        groups = {}
        failed = []
        for raw_src, raw_dst, extra in pairs:
            src = normalize_cloud(raw_src)
            if src == "/":
                # 复制根目录等于把整个存储再拷一份，必然是误操作
                failed.append({"path": raw_src, "error": "不允许复制云端根目录"})
                continue
            src_dir, _, src_name = src.rpartition("/")
            if raw_dst:
                dst = normalize_cloud(raw_dst)
                dst_dir, _, dst_name = dst.rpartition("/")
                if dst_name != src_name:
                    # OpenList 的复制接口只收「源目录 + 名字 + 目标目录」，
                    # 落点的名字恒等于源名 —— 改名它做不到，如实报错，
                    # 不能让用户以为文件复制到别处去了。
                    failed.append({"path": raw_src, "error":
                                   "云端复制不支持改名（%s → %s）"
                                   % (src_name, dst_name)})
                    continue
            else:
                dst_dir = target_dir
            groups.setdefault((src_dir or "/", dst_dir or "/"), []).append(
                (src_name, src, extra))

        client = self.ctx.client()
        copied, tasks = 0, []
        # 同步完成（OpenList 不建后台任务）的复制，回执落到传输记录里 ——
        # 否则这些复制在传输列表里没有任何痕迹（BUG-3）。
        sync_copies = []
        for (src_dir, dst_dir), members in groups.items():
            names = [n for n, _, _ in members]
            try:
                data = client.copy(src_dir, dst_dir, names, overwrite=True)
                copied += len(names)
                task_ids = _task_ids(data)
                tasks.extend(task_ids)
                if not task_ids:
                    for name, _path, extra in members:
                        sync_copies.append((src_dir, dst_dir, name,
                                            _size_of(extra),
                                            bool((extra or {}).get("isDir"))))
            except openlist.OpenlistError as exc:
                # 整组失败 → 退化成逐条重试：一个条目出错（目标被权限锁住、
                # 名字非法…）不该让同组其它条目陪葬；而且只有逐条才知道是谁失败。
                self.ctx.log("[api] 云端复制整组失败（%s → %s），改为逐条重试：%s"
                             % (src_dir, dst_dir, exc))
                for name, path, extra in members:
                    try:
                        data = client.copy(src_dir, dst_dir, [name], overwrite=True)
                        copied += 1
                        task_ids = _task_ids(data)
                        tasks.extend(task_ids)
                        if not task_ids:
                            sync_copies.append((src_dir, dst_dir, name,
                                                _size_of(extra),
                                                bool((extra or {}).get("isDir"))))
                    except openlist.OpenlistError as exc2:
                        failed.append({"path": path, "error": str(exc2)})

        self._record_cloud_copies(sync_copies)
        self.ctx.log("已在云端复制 %d 项（落点：%s）"
                     % (copied, target_dir or "各自的原路径"))
        self._json({"ok": True, "side": SOURCE_CLOUD, "target": target_dir,
                    "copied": copied, "failed": failed, "tasks": tasks})

    def _copy_local(self, pairs, target):
        roots = config.accessible_paths()
        root_norms = _local_roots_norm(roots)
        target_dir = os.path.abspath(target) if target else ""

        # 两趟走：**先把能判的错全判完，再动文件系统**。目标是源的子目录这种
        # 误操作一旦先建了目录，用户会凭空多出一个空文件夹，还得自己去删。
        plans, failed = [], []
        # 传输记录：本地复制**不产生任何队列/后台任务**，不记就什么都看不到（BUG-3）。
        # 只在**真的进入复制阶段**之后才落记录 —— 预检挡下的条目（源不存在、源即
        # 目标、目标是源子目录、越界、复制存储根目录）只写日志：它们是"参数不对"，
        # 不是"复制失败了"，而且反复误操作会迅速填满 500 条上限，把真正的成功
        # 记录挤掉。这些错误已经通过 `failed` 逐条回给前端（弹层里列得清清楚楚）。
        records = []
        for raw_src, raw_dst, extra in pairs:
            src = os.path.abspath(str(raw_src))
            # 落点：带 dst 就**精确落位**（比对场景，相对层级必须保住），
            # 否则沿用旧的"按名字放进 target 目录"。
            dst = os.path.abspath(str(raw_dst)) if raw_dst \
                else os.path.join(target_dir, os.path.basename(src.rstrip(os.sep)))
            if not is_path_allowed(src, roots):
                failed.append({"path": str(raw_src), "error": "源路径不在允许访问的范围内"})
                continue
            parent = os.path.dirname(dst)
            if not is_path_allowed(parent, roots):
                # 白名单外是配置级错误，整批拒绝 —— 与旧版"目标目录不在允许访问
                # 范围内"的处理一致，不能让用户以为只是这一项被跳过。
                raise ApiError("目标目录不在允许访问的范围内：%s" % parent)
            if os.path.normcase(src) in root_norms:
                failed.append({"path": src, "error": "不允许复制存储空间根目录"})
                continue
            if not os.path.lexists(src):
                failed.append({"path": src, "error": "源路径不存在"})
                continue
            if os.path.normcase(src) == os.path.normcase(dst):
                failed.append({"path": src, "error": "源路径就是目标路径"})
                continue
            if os.path.isdir(src) and not os.path.islink(src):
                # 目标落在源里面 → 复制会把自己套进自己，一路递归下去。
                # 复用 `is_path_allowed` 判包含关系（它本来就是"在不在某个 root 内"）。
                if is_path_allowed(dst, [src]):
                    failed.append({"path": src, "error":
                                   "目标目录在源目录内部，会无限自我复制"})
                    continue
            plans.append((src, dst, extra))

        if failed:
            # 预检失败只留痕在日志里（前端已经从 failed 里拿到逐条原因）
            self.ctx.log("本地复制预检跳过 %d 项：%s"
                         % (len(failed), failed[0].get("error", "")))

        copied = 0
        if plans:
            try:
                # 落点按相对层级分散在不同目录里 → 逐个建父目录（去重后建）。
                for parent in sorted({os.path.dirname(d) for _, d, _ in plans}):
                    os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise ApiError("创建目标目录失败：%s" % exc)
            for src, dst, extra in plans:
                try:
                    is_dir = os.path.isdir(src) and not os.path.islink(src)
                    if is_dir:
                        # dirs_exist_ok：目标已有同名目录时**合并**进去而不是报错 ——
                        # 与云端 copy(overwrite=True) 的语义对齐（同名覆盖，其余保留）。
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    copied += 1
                    records.append(_local_record(src, dst, extra, "", is_dir=is_dir))
                except (OSError, shutil.Error) as exc:
                    failed.append({"path": src, "error": str(exc)})
                    records.append(_local_record(src, dst, extra, str(exc)))

        if records:
            self.ctx.records().add_many(records)
        self.ctx.log("已在本地复制 %d 项（落点：%s）"
                     % (copied, target_dir or "各自的原路径"))
        # `tasks` 恒为空：本地复制不产生任何后台任务（保留字段是为了响应形状统一）
        self._json({"ok": True, "side": SOURCE_LOCAL, "target": target_dir,
                    "copied": copied, "failed": failed, "tasks": [],
                    "records": len(records)})

    # -- 差异清理：删除多余文件（不可逆）--

    def h_cloud_remove(self, query, body):
        """删除云端路径（文件或目录，OpenList 侧目录是递归删除）。

        这是**不可逆**操作，接口本身不提供"撤销"；服务端是否进回收站取决于
        OpenList 存储驱动的配置。前端必须先做一次明确的二次确认。
        """
        paths = _str_list(body.get("paths"))
        if not paths:
            raise ApiError("没有指定要删除的云端路径")
        removed, failed = self.ctx.client().remove_many(paths)
        self.ctx.audit("cloud_remove", count=len(removed), paths=removed[:500],
                       failed=[f.get("path") for f in failed[:100]])
        if removed:
            self.ctx.log("已删除 %d 个云端路径" % len(removed))
        self._json({"ok": True, "removed": len(removed), "paths": removed,
                    "failed": failed})

    def h_local_remove(self, query, body):
        """删除本地路径（文件或目录树）。

        安全边界（缺一不可）：
        * 必须在 `accessible_paths()` 白名单内 —— 默认就是 `/vol*`，越界直接拒；
        * 拒绝删除白名单根目录本身（`rmtree('/vol1')` 是灾难级误操作）；
        * 逐条独立处理，一条失败不影响其余，并把原因带回去。
        """
        paths = _str_list(body.get("paths"))
        if not paths:
            raise ApiError("没有指定要删除的本地路径")
        roots = config.accessible_paths()
        root_norms = _local_roots_norm(roots)

        removed, failed = [], []
        for raw in paths:
            target = os.path.abspath(raw)
            if not is_path_allowed(target, roots):
                failed.append({"path": raw, "error": "路径不在允许访问的范围内"})
                continue
            if os.path.normcase(target) in root_norms:
                failed.append({"path": raw, "error": "不允许删除存储空间根目录"})
                continue
            if not os.path.lexists(target):
                failed.append({"path": target, "error": "路径已不存在"})
                continue
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.unlink(target)
                removed.append(target)
            except OSError as exc:
                failed.append({"path": target, "error": str(exc)})

        self.ctx.audit("local_remove", count=len(removed), paths=removed[:500],
                       failed=[f.get("path") for f in failed[:100]])
        if removed:
            self.ctx.log("已删除 %d 个本地路径" % len(removed))
        self._json({"ok": True, "removed": len(removed), "paths": removed,
                    "failed": failed})

    # -- 下载队列 --

    def h_download_enqueue(self, query, body):
        """把云端文件/目录加入下载队列。

        目录会在服务端**展开成具体文件**再入队：下载队列只搬单个文件，
        把目录塞进去只会得到一个"这是目录，不能直接下载"的失败任务。
        """
        cfg = self.ctx.cfg()
        roots = config.accessible_paths()
        excludes = cfg.get("excludeNames")
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError("没有指定要下载的内容")

        clean = []
        dropped = 0
        for item in items:
            if not isinstance(item, dict):
                dropped += 1
                continue
            remote = normalize_cloud(item.get("remotePath") or "")
            raw_local = (item.get("localPath") or "").strip()
            if not remote or not raw_local:
                dropped += 1
                continue
            local = os.path.abspath(raw_local)
            if not is_path_allowed(local, roots):
                raise ApiError("本地路径不在允许访问的范围内：%s" % local)

            if item.get("isDir"):
                # 云端目录 → 递归展开成文件清单，落到本地同名目录下。
                # 用「不限深度」：用户点的是"下载这个目录"，漏掉深层文件
                # 会变成一个静默少下的半成品。
                sub = verify_mod.scan_cloud(
                    self.ctx.client(), remote, DEPTH_UNLIMITED, excludes,
                    concurrency=verify_mod.normalize_concurrency(
                        cfg.get("scanConcurrency")))
                for rel in sorted(sub.keys()):
                    node = sub[rel]
                    if node.get("isDir"):
                        continue
                    clean.append({
                        "remotePath": join_cloud(remote, rel),
                        "localPath": os.path.join(local, *rel.split("/")),
                        "size": int(node.get("size") or 0),
                    })
                continue

            try:
                size = max(0, int(item.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            clean.append({"remotePath": remote, "localPath": local, "size": size})

        if not clean:
            raise ApiError("没有可下载的文件（可能被排除规则过滤，或是空目录）")
        added = self.ctx.downloads().add(clean, overwrite=True)
        if dropped:
            self.ctx.log("下载入队时跳过 %d 个不完整条目" % dropped)
        self._json({"ok": True, "added": len(added), "tasks": added,
                    "counts": self.ctx.downloads().counts()})

    # -- 上传队列 --

    def h_upload_enqueue(self, query, body):
        cfg = self.ctx.cfg()
        roots = config.accessible_paths()
        cloud_dir = normalize_cloud(body.get("cloudDir") or cfg.get("cloudRoot") or "/")
        excludes = cfg.get("excludeNames")

        items = []
        dropped = 0
        items_in = body.get("items")
        local_paths = body.get("localPaths")

        if isinstance(items_in, list) and items_in:
            # 对比模式：前端已算好 localPath → remotePath 映射
            for it in items_in:
                if not isinstance(it, dict):
                    dropped += 1
                    continue
                lp = (it.get("localPath") or "").strip()
                rp = (it.get("remotePath") or "").strip()
                if not lp or not rp:
                    dropped += 1
                    continue
                if not is_path_allowed(lp, roots):
                    raise ApiError("本地路径不在允许访问的范围内：%s" % lp)
                if os.path.isfile(lp):
                    items.append({"localPath": lp, "remotePath": normalize_cloud(rp)})
                elif os.path.isdir(lp):
                    # 比对结果里的「仅本地目录」：云端路径就是该目录自己的落点，
                    # 直接把内容铺进去（include_base=False），不再套一层目录名。
                    items.extend(expand_local_dir(lp, normalize_cloud(rp), excludes,
                                                  include_base=False))
                else:
                    dropped += 1
        elif isinstance(local_paths, list) and local_paths:
            # 浏览模式：给一批本地文件/目录 + 一个云端目标目录，后端展开
            for raw in local_paths:
                lp = os.path.abspath(str(raw))
                if not is_path_allowed(lp, roots):
                    raise ApiError("本地路径不在允许访问的范围内：%s" % lp)
                if os.path.isfile(lp):
                    try:
                        size = int(os.stat(lp).st_size)
                    except OSError:
                        size = 0
                    items.append({"localPath": lp,
                                  "remotePath": join_cloud(cloud_dir, os.path.basename(lp)),
                                  "size": size})
                elif os.path.isdir(lp):
                    items.extend(expand_local_dir(lp, cloud_dir, excludes))
                else:
                    raise ApiError("路径不存在：%s" % lp)
        else:
            raise ApiError("没有指定要上传的内容")

        if not items:
            raise ApiError("没有可上传的文件（可能被排除规则过滤，或是空目录）")

        added = self.ctx.queue().add(items, skip_existing=cfg.get("skipExisting"))
        if dropped:
            # 被丢弃的条目必须留痕：前端只看到"新增 0 个"时用户无从判断原因
            self.ctx.log("入队时跳过 %d 个条目（路径不存在或不完整）" % dropped)
        self._json({"ok": True, "added": len(added), "tasks": added,
                    "dropped": dropped, "counts": self.ctx.queue().counts()})

    def h_upload_list(self, query, body):
        q = self.ctx.queue()
        tasks = q.list()
        # 绑定备份的 copy 任务不再在这里合并：传输列表直接引用 OpenList 的任务列表
        # （/api/openlist/tasks），由 OpenList 说了算运行中/成功/失败。
        # 下载任务同样进这一个列表：用户心里的"传输列表"就是"我让它干的活"，
        # 分成两个页面看反而要多点一次。靠 direction 区分渲染。
        try:
            tasks = tasks + self.ctx.downloads().list()
        except Exception as exc:
            self.ctx.log("[api] 合并下载任务失败：%r" % (exc,))
        # 「传输记录」：本地复制 / 同存储云端复制这两条链路既不进队列也没有
        # OpenList 任务，不合并进来传输列表就一片空白（BUG-3：本地传本地看不到）。
        try:
            tasks = tasks + self.ctx.records().list()
        except Exception as exc:
            self.ctx.log("[api] 合并传输记录失败：%r" % (exc,))
        self._json({"ok": True, "tasks": tasks, "counts": q.counts(),
                    "paused": q.is_paused(),
                    "pausedDownload": self.ctx.downloads().is_paused()})

    def h_upload_retry(self, query, body):
        # 一键重试：**本地两条队列 + OpenList 两类任务的失败项**。
        # 旧实现只重试本地队列，而「全部重试」按钮的显示条件算的是合并后的失败数
        # —— 于是「本地 0 失败 + OpenList 有失败」时按钮会出现，点下去却什么都不
        # 会重试，这正是用户报的「缺少全部重试失败项按钮」。
        if body.get("all"):
            retried = self.ctx.queue().retry_all() + self.ctx.downloads().retry_all()
            ol = self._retry_openlist_failed()
            self._json({"ok": True, "retried": retried,
                        "openlistRetried": ol["retried"],
                        "openlistRetriedKnown": ol["known"],
                        "openlistTypes": ol["byType"]})
            return
        # 单条重传：body {"id": "..."}（任务不存在仍 404）
        queue, task = self.ctx.find_transfer(body.get("id") or "")
        if not queue:
            raise ApiError("任务不存在", 404)
        self._json({"ok": True, "task": queue.retry(task["id"])})

    def h_upload_cancel(self, query, body):
        queue, task = self.ctx.find_transfer(body.get("id") or "")
        if not queue:
            raise ApiError("任务不存在", 404)
        self._json({"ok": True, "task": queue.cancel(task["id"])})

    def h_upload_remove(self, query, body):
        task_id = str(body.get("id") or "").strip()
        if not task_id:
            raise ApiError("缺少任务 id")
        # 传输记录（本地复制 / 同存储云端复制）不在任何队列里，先按 id 找记录。
        # 记录 id 前缀 lc_ / olc_ 与队列任务 id 不冲突，按"先记录后队列"找不会误删。
        try:
            if self.ctx.records().remove(task_id):
                self._json({"ok": True, "record": True})
                return
        except Exception as exc:
            self.ctx.log("[api] 删除传输记录失败：%r" % (exc,))
        queue, task = self.ctx.find_transfer(task_id)
        if not queue:
            raise ApiError("任务不存在", 404)
        queue.remove(task["id"])
        self._json({"ok": True})

    def h_upload_clear(self, query, body):
        # 清除本地队列已结束 + 传输记录 + OpenList 已完成任务
        local_removed = self.ctx.queue().clear_done() + self.ctx.downloads().clear_done()
        records_removed = 0
        try:
            records_removed = self.ctx.records().clear_terminal()
        except Exception as exc:
            self.ctx.log("清除传输记录失败：%s" % exc)
        ol_cleared = False
        try:
            client = self.ctx.client()
            if client:
                ol_cleared = client.clear_done_tasks()
        except Exception as exc:
            self.ctx.log("清除 OpenList 已完成任务失败：%s" % exc)
        self._json({"ok": True, "removed": local_removed + records_removed,
                    "localRemoved": local_removed,
                    "recordsRemoved": records_removed,
                    "openlistCleared": ol_cleared})

    def h_upload_pause(self, query, body):
        """暂停 / 恢复。

        **只作用于本地队列**：OpenList 没有「暂停」能力，只有「取消」。
        旧实现在暂停时顺手把 OpenList 排队中的任务取消掉了 —— 暂停于是变成
        不可逆（恢复后那些任务再也回不来，文件少了一批），用户看到的就是
        「暂停没生效 / 恢复后东西少了」。这个副作用已去掉，前端的 toast 也会
        明确说明「OpenList 任务不支持暂停，要停请用全部取消」。
        """
        paused = bool(body.get("paused"))
        # 一个按钮管两条队列：用户点「全部暂停」时不会去想"上传和下载是两回事"
        self.ctx.queue().set_paused(paused)
        self.ctx.downloads().set_paused(paused)
        self._json({"ok": True, "paused": paused,
                    "pausedDownload": self.ctx.downloads().is_paused(),
                    # 明确告诉前端：暂停对 OpenList 无效（别在文案里承诺做不到的事）
                    "openlistPausable": False})

    def h_upload_cancel_all(self, query, body):
        """取消全部：本地两条队列 + OpenList 里**传输列表可见类型**的未完成项。

        四件必须做对的事（缺一条都会退回"点了没反应 / 谎报成功"）：

        1. **按类型取任务**：`/api/task/<type>/undone` 是按类型分段的，类型取错
           就一个都取消不到。取哪些类型由 `VISIBLE_TASK_TYPES` 决定 —— 只动
           用户在传输列表里看得见的那类，绝不按 `openlist.TASK_TYPES` 全量去扫；
        2. **回读验证**：批量接口 HTTP 200 不代表成功（见
           `openlist.parse_batch_errors`），取消完必须再拉一次 undone 才知道
           到底停了没有；
        3. **逐条兜底**：残留的任务逐个用单条接口再取消一次 —— 批量接口对
           「不存在的 tid」只会在 data 里静静写一句错误；
        4. **如实回报**：残留几个就说几个，绝不按"请求发成功了"算成功数。

        另外 `cancel_all()` 会解除本地队列的暂停态：取消完最自然的下一步是
        「重试」，而暂停态下 worker 根本不取任务，点了重试什么都不会发生。
        """
        was_paused = self.ctx.queue().is_paused() or self.ctx.downloads().is_paused()
        local_cancelled = self.ctx.queue().cancel_all() + self.ctx.downloads().cancel_all()

        report = {"requested": 0, "cancelled": 0, "finished": 0,
                  "remaining": 0, "byType": {}}
        try:
            client = self.ctx.client()
            if client.is_configured():
                report = _cancel_openlist_undone(client, self.ctx.log)
        except Exception as exc:
            self.ctx.log("取消 OpenList 任务失败：%s" % exc)
            report["error"] = str(exc)

        self._json({"ok": True, "cancelled": local_cancelled,
                    "openlistCancelled": report.get("cancelled", 0),
                    "openlistFinished": report.get("finished", 0),
                    "openlistRequested": report.get("requested", 0),
                    "openlistRemaining": report.get("remaining", 0),
                    "openlistTypes": report.get("byType", {}),
                    "openlistError": report.get("error", ""),
                    # 本地队列的暂停态在这次操作里被解除了（前端据此提示）
                    "unpaused": bool(was_paused and not self.ctx.queue().is_paused()),
                    "paused": self.ctx.queue().is_paused()})

    def _retry_openlist_failed(self):
        """让 OpenList 重试**传输列表可见类型**里的失败项，返回分项计数。

        与 cancel_all 同一条口径（`VISIBLE_TASK_TYPES`）：列表里看不到的任务类型，
        「全部重试」也不替它做主。

        单个类型失败（服务端不支持 / 网络抖动）静默跳过并记日志 —— 不能因为
        某一类不可用就把整个「全部重试」判失败。

        `retry_failed` 接口**不返回重试条数**，只能做「调用前后各数一次」的差；
        任一侧拿不到列表就把 retried 置 None（前端如实说"条数无法核实"）。
        """
        out = {"retried": 0, "known": True, "byType": {}}
        try:
            client = self.ctx.client()
        except Exception as exc:
            self.ctx.log("重试 OpenList 失败项时取不到客户端：%s" % exc)
            return {"retried": 0, "known": False, "byType": {}}
        if not client.is_configured():
            # 没连 OpenList 不是错误：本地队列照样重试过了
            return {"retried": 0, "known": False, "byType": {}}
        for task_type in VISIBLE_TASK_TYPES:
            try:
                result = client.retry_failed_tasks(task_type)
            except Exception as exc:
                self.ctx.log("重试 OpenList 失败项出错（%s）：%s" % (task_type, exc))
                result = {"ok": False, "taskType": task_type, "retried": None,
                          "error": str(exc)}
            out["byType"][task_type] = result
            if result.get("retried") is None:
                out["known"] = False
            else:
                out["retried"] += int(result.get("retried") or 0)
        return out

    # -- OpenList 服务端任务：单条操作 --
    # 传输列表里的 OpenList 行以前只有一个「OpenList 任务」文字标签，用户对这类
    # 任务**一个按钮都没有**。这三个薄接口就是那三个按钮的落点。
    # 沿用 `ol_<tid>` 的 id 约定；`type` 不给就 copy → upload 依次试，前端不必关心。

    OPENLIST_TASK_ACTIONS = {"cancel": "取消", "retry": "重试", "delete": "删除"}

    def h_openlist_task_cancel(self, query, body):
        self._openlist_task_action("cancel", body)

    def h_openlist_task_retry(self, query, body):
        self._openlist_task_action("retry", body)

    def h_openlist_task_delete(self, query, body):
        self._openlist_task_action("delete", body)

    def _openlist_task_action(self, action, body):
        client = self.ctx.client()
        if not client.is_configured():
            raise ApiError("尚未连接 OpenList，无法操作服务端任务", status=401)
        tid, task_type = split_openlist_task_id(body.get("id"), body.get("type"))
        if not tid:
            raise ApiError("缺少任务 id")
        label = self.OPENLIST_TASK_ACTIONS.get(action, action)
        types = [task_type] if task_type else list(openlist.TASK_TYPES)
        for kind in types:
            if client.task_action(action, tid, kind):
                self.ctx.log("已%s OpenList %s 任务：%s" % (label, kind, tid))
                self._json({"ok": True, "action": action, "id": tid,
                            "type": kind, "count": 1})
                return
        # 全类型都失败 → 如实报错，别假装成功（任务可能已被服务端清理掉）
        raise ApiError("OpenList 任务%s失败（任务可能已被清理或已结束）：%s"
                       % (label, tid), 404)

    # -- 目录绑定 --

    def h_bindings_list(self, query, body):
        # 每次列表请求时同步队列状态
        store = self.ctx.bindings()
        store.refresh_statuses()
        self._json({"ok": True, "bindings": store.list_bindings()})

    def h_bindings_detail(self, query, body):
        binding_id = _arg(query, "id", "") or ""
        store = self.ctx.bindings()
        store.refresh_statuses(binding_id)
        data = store.get_binding(binding_id)
        if not data:
            raise ApiError("绑定不存在", 404)
        self._json({"ok": True, "binding": data})

    def h_bindings_create(self, query, body):
        store = self.ctx.bindings()
        try:
            data = store.create_binding(body)
        except ValueError as exc:
            raise ApiError(str(exc))
        self._json({"ok": True, "binding": data})

    def h_bindings_update(self, query, body):
        binding_id = body.get("id") or ""
        store = self.ctx.bindings()
        try:
            data = store.update_binding(binding_id, body)
        except ValueError as exc:
            raise ApiError(str(exc))
        if not data:
            raise ApiError("绑定不存在", 404)
        self._json({"ok": True, "binding": data})

    def h_bindings_delete(self, query, body):
        binding_id = body.get("id") or ""
        store = self.ctx.bindings()
        if not store.delete_binding(binding_id):
            raise ApiError("绑定不存在", 404)
        self._json({"ok": True})

    def h_bindings_scan(self, query, body):
        binding_id = body.get("id") or ""
        store = self.ctx.bindings()
        ok, reason = store.can_scan(binding_id)
        if not ok:
            if reason == "绑定不存在":
                raise ApiError("绑定不存在", 404)
            raise ApiError(reason)
        store.start_scan(binding_id)
        self._json({"ok": True})

    def h_bindings_scan_all(self, query, body):
        store = self.ctx.bindings()
        started, skipped = store.scan_all()
        self._json({"ok": True, "started": started, "skipped": skipped})

    def h_bindings_upload(self, query, body):
        binding_id = body.get("id") or ""
        rel_paths = body.get("relPaths")
        target_ids = body.get("targetIds")
        upload_all = bool(body.get("all"))
        # 传字符串进来会让 `rel not in rel_paths` 变成子串匹配（"a.txt" in "xa.txtb"），
        # 顺手传错类型就静默提交错文件，这里必须挡住。
        if rel_paths is not None and not isinstance(rel_paths, list):
            raise ApiError("relPaths 必须是数组")
        if target_ids is not None and not isinstance(target_ids, list):
            raise ApiError("targetIds 必须是数组")
        store = self.ctx.bindings()
        if not store.get_binding(binding_id):
            raise ApiError("绑定不存在", 404)
        # asyncMode：前端新调用方式，校验通过后立刻返回，提交丢后台线程。
        # 不传则保持原同步行为（旧客户端 / 契约测试依赖同步返回的 queued）。
        if bool(body.get("asyncMode")):
            self._spawn_submit(store, binding_id, rel_paths, target_ids, upload_all)
            self._json({"ok": True, "accepted": True, "id": binding_id})
            return
        try:
            count = store.upload_files(
                binding_id, rel_paths=rel_paths,
                target_ids=target_ids, upload_all=upload_all)
        except ValueError as exc:
            raise ApiError(str(exc))
        if count is None:
            raise ApiError("绑定不存在", 404)
        # 把「这次提交」的最终结论一并回给前端（提交了多少、几个目录失败、
        # 一句人话说明），省得它为了一句结果再去问一次进度接口。
        progress = store.get_progress(binding_id, submit_only=True) or {}
        self._json({"ok": True, "queued": count,
                    "submit": progress.get("submit") or {}})

    def _spawn_submit(self, store, binding_id, rel_paths, target_ids, upload_all):
        """把一次提交丢到后台线程，立即返回。幂等闸门在 upload_files 内部。"""
        def _run():
            try:
                store.upload_files(
                    binding_id, rel_paths=rel_paths,
                    target_ids=target_ids, upload_all=upload_all)
            except Exception as exc:
                # upload_files 内部已做异常收尾（会写 phase=error）；这里兜底记日志，
                # 绝不让线程里的异常静默消失。
                try:
                    self.ctx.log("异步提交后台异常：%s" % exc)
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True,
                         name="submit-%s" % str(binding_id)[:12]).start()

    def h_bindings_progress(self, query, body):
        # 必须用 _arg 取首个值：parse_qs 会把所有值包成 list，直接
        # `(query or {}).get("id")` 拿到的是 ["bd_xxx"] —— 拿去查绑定永远查不到，
        # 这个接口会一直返回 progress:null，前端的进度轮询完全是白转。
        binding_id = _arg(query, "id", "") or ""
        store = self.ctx.bindings()
        # submit=1：只要「提交阶段」的进度 —— 纯内存、不发任何网络请求。
        # 前端在提交进行中每秒刷好几次进度条，走的就是这条轻量路径。
        prog = store.get_progress(
            binding_id, submit_only=_as_bool(_arg(query, "submit", "")))
        self._json({"ok": True, "progress": prog})

    # -- 日志 / favicon --

    def h_openlist_tasks(self, query, body):
        """直接返回 OpenList 的 copy 任务列表（运行中 + 最近完成）。

        传输列表不再自己逐个文件探测成功/失败，而是直接引用 OpenList 的任务状态：
        OpenList 说在跑就是在跑，说成功就是成功，说失败就是失败。
        """
        store = self.ctx.bindings()
        client = getattr(store, "client", None)
        if client is None:
            self._json({"ok": True, "tasks": [], "error": "OpenList 未连接"})
            return
        data = client.list_tasks("copy")
        if data is None:
            self._json({"ok": True, "tasks": [], "error": "查询 OpenList 任务失败"})
            return
        # OpenList 后台任务状态（pkg/tache/state.go，iota 顺序）：
        # 0=Pending 排队，1=Running 运行中，2=Succeeded 成功，3=Canceling 取消中，
        # 4=Canceled 已取消，5=Errored 出错待重试，6=Failing 失败收尾，7=Failed 彻底失败。
        # 2.10.2 修复：此前 0/1 写反 —— 服务端还在排队的任务（state=0）被显示成
        # 「上传中 0%」，真正在跑的任务（state=1）反而显示「排队中」；批量复制时
        # 两批任务一交错，界面状态就来回横跳。
        STATE_MAP = {0: "pending", 1: "uploading", 2: "done", 3: "cancelled",
                     4: "cancelled", 5: "pending", 6: "failed", 7: "failed"}
        tasks = []
        missing_id = 0
        # 运行中的放前面
        for tk in data.get("undone") or []:
            if not isinstance(tk, dict):
                continue
            tid = str(tk.get("id") or "").strip()
            if not tid:
                # 没有 id 的任务对前端毫无用处：它的 id 会塌成同一个 "ol_"，
                # 前端按 id 去重后这些行会**静默消失**；更糟的是单条操作接口
                # 拿到 "ol_" 也定位不到任何任务。这里直接跳过并计数。
                missing_id += 1
                continue
            state = int(tk.get("state") or 0)
            tasks.append({
                "id": OL_TASK_ID_PREFIX + tid,
                "name": tk.get("name") or "复制任务",
                "remotePath": "OpenList 服务端复制",
                "size": int(tk.get("total_bytes") or tk.get("total") or 0),
                "uploaded": 0,
                "status": STATE_MAP.get(state, "pending"),
                "progress": int(tk.get("progress") or 0),
                "error": tk.get("error") or "",
                "speed": 0,
                "isOpenList": True,
                "openListId": tid,
            })
        # 最近完成的（最多 3000 条）
        done_list = data.get("done") or []
        done_list = done_list[-3000:]
        for tk in done_list:
            if not isinstance(tk, dict):
                continue
            tid = str(tk.get("id") or "").strip()
            if not tid:
                missing_id += 1
                continue
            state = int(tk.get("state") or 2)
            tasks.append({
                "id": OL_TASK_ID_PREFIX + tid,
                "name": tk.get("name") or "复制任务",
                "remotePath": "OpenList 服务端复制",
                "size": int(tk.get("total_bytes") or tk.get("total") or 0),
                "uploaded": int(tk.get("total_bytes") or tk.get("total") or 0) if state == 2 else 0,
                "status": STATE_MAP.get(state, "done"),
                "progress": 100 if state == 2 else int(tk.get("progress") or 0),
                "error": tk.get("error") or "",
                "speed": 0,
                "isOpenList": True,
                "openListId": tid,
            })
        if missing_id:
            # 只记一条汇总日志：每 2 秒轮询一次，逐条打日志会把 info.log 冲爆
            self.ctx.log("[api] 跳过 %d 个没有 id 的 OpenList 任务（无法定位/去重）"
                         % missing_id)
        self._json({"ok": True, "tasks": tasks})

    def h_log(self, query, body):
        """读取运行日志。支持 ?lines=N 指定行数（默认500，最大2000），
        ?level=error|warn 只返回包含对应关键词的行。"""
        def _first(v):
            """query 参数可能是列表（重复参数时），取第一个；不是列表直接返回。"""
            if isinstance(v, (list, tuple)):
                return v[0] if v else None
            return v
        try:
            n = int(_first(query.get("lines")) or 500)
        except (TypeError, ValueError):
            n = 500
        n = max(50, min(2000, n))
        level = str(_first(query.get("level")) or "").lower()
        try:
            lines = _tail_lines(config.log_path(), n)
        except OSError:
            lines = []
        parsed = []
        for line in lines:
            text = line.rstrip("\n")
            # 解析级别：日志内容里包含 ERROR/WARN/失败/错误 等关键词
            lvl = "info"
            low = text.lower()
            if any(k in low for k in ("error", "失败", "错误", "exception", "traceback")):
                lvl = "error"
            elif any(k in low for k in ("warn", "警告", "skip", "跳过")):
                lvl = "warn"
            if level and lvl != level:
                continue
            # 提取时间戳 [YYYY-MM-DD HH:MM:SS]
            ts = ""
            if text.startswith("[") and "]" in text[:22]:
                ts = text[1:20]
                text = text[22:].strip()
            parsed.append({"t": ts, "l": lvl, "m": text})
        self._json({"ok": True, "lines": parsed, "total": len(parsed)})

    def h_log_clear(self, query, body):
        """清空运行日志。"""
        try:
            with open(config.log_path(), "w", encoding="utf-8") as fh:
                fh.write("")
        except OSError:
            pass
        self._json({"ok": True})

    def h_favicon(self, query, body):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


# --------------------------------------------------------------------------
# 路由表
# --------------------------------------------------------------------------

_ROUTES = {
    ("GET", "/api/info"): "h_info",
    ("GET", "/api/status"): "h_status",
    ("GET", "/api/config"): "h_config_get",
    ("POST", "/api/config"): "h_config_set",
    ("POST", "/api/connect"): "h_connect",
    ("POST", "/api/connect/test"): "h_connect_test",
    ("POST", "/api/disconnect"): "h_disconnect",
    ("GET", "/api/connection"): "h_connection",
    ("GET", "/api/local/roots"): "h_local_roots",
    ("GET", "/api/local/list"): "h_local_list",
    ("GET", "/api/cloud/list"): "h_cloud_list",
    ("POST", "/api/cloud/mkdir"): "h_cloud_mkdir",
    ("POST", "/api/cloud/copy"): "h_cloud_copy",
    ("POST", "/api/compare"): "h_compare",
    ("POST", "/api/sync/copy"): "h_sync_copy",
    ("POST", "/api/cloud/remove"): "h_cloud_remove",
    ("POST", "/api/local/remove"): "h_local_remove",
    ("POST", "/api/download/enqueue"): "h_download_enqueue",
    ("POST", "/api/upload/enqueue"): "h_upload_enqueue",
    ("GET", "/api/upload/list"): "h_upload_list",
    ("POST", "/api/upload/retry"): "h_upload_retry",
    ("POST", "/api/upload/cancel"): "h_upload_cancel",
    ("POST", "/api/upload/remove"): "h_upload_remove",
    ("POST", "/api/upload/clear"): "h_upload_clear",
    ("POST", "/api/upload/pause"): "h_upload_pause",
    ("POST", "/api/upload/cancel_all"): "h_upload_cancel_all",
    # OpenList 服务端任务的单条操作（body {"id": "ol_<tid>"[, "type"]}）
    ("POST", "/api/openlist/task/cancel"): "h_openlist_task_cancel",
    ("POST", "/api/openlist/task/retry"): "h_openlist_task_retry",
    ("POST", "/api/openlist/task/delete"): "h_openlist_task_delete",
    # 目录绑定
    ("GET", "/api/bindings"): "h_bindings_list",
    ("GET", "/api/bindings/detail"): "h_bindings_detail",
    ("POST", "/api/bindings/create"): "h_bindings_create",
    ("POST", "/api/bindings/update"): "h_bindings_update",
    ("POST", "/api/bindings/delete"): "h_bindings_delete",
    ("POST", "/api/bindings/scan"): "h_bindings_scan",
    ("POST", "/api/bindings/scan-all"): "h_bindings_scan_all",
    ("POST", "/api/bindings/upload"): "h_bindings_upload",
    ("GET", "/api/bindings/progress"): "h_bindings_progress",
    ("GET", "/api/openlist/tasks"): "h_openlist_tasks",
    ("GET", "/api/log"): "h_log",
    ("POST", "/api/log/clear"): "h_log_clear",
    ("GET", "/api/favicon.ico"): "h_favicon",
}


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _aggregate_roots(roots):
    """把各存储空间的用户目录（`/vol*/1000`）聚合成一层，供左栏根视图展示。"""
    entries = []
    for root in roots:
        user_dir = os.path.join(root, "1000")
        base = user_dir if os.path.isdir(user_dir) else root
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.name.startswith(".") or entry.name.startswith("@"):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if not is_dir:
                        continue
                    entries.append({"name": entry.name, "path": entry.path,
                                    "isDir": True, "size": 0,
                                    "volume": os.path.basename(root)})
        except OSError:
            continue
    entries.sort(key=lambda e: (e["name"].lower(), e["volume"]))
    counts = {}
    for entry in entries:
        counts[entry["name"]] = counts.get(entry["name"], 0) + 1
    for entry in entries:
        entry["nameDup"] = counts[entry["name"]]
    return entries


def _lan_ip():
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("192.168.0.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "NAS_IP"


def _transport_note():
    return httpclient.detect_backend()


_APP_CONTEXT = None
_CURRENT_LOG = None


def set_app_context(ctx, log=None):
    global _APP_CONTEXT, _CURRENT_LOG
    _APP_CONTEXT = ctx
    _CURRENT_LOG = log or getattr(ctx, "log", None)


# --------------------------------------------------------------------------
# 服务启动
# --------------------------------------------------------------------------

def _unix_socket_alive(socket_path):
    """socket 文件还在，是否真有实例在监听。"""
    import socket as _socket
    if not hasattr(_socket, "AF_UNIX"):
        return False
    sock = None
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def create_unix_server(socket_path, ctx):
    """统一网关模式：监听 Unix Socket。Windows 无 UnixStreamServer，类定义条件化。

    单实例保护：早先无条件 `os.unlink(socket_path)` 再 bind —— 第二个实例启动
    会把正在运行那个实例的 socket 路径摘掉，新旧两个进程各自持有一份
    queue.json / bindings.json，互相覆盖（症状是"刚加的任务自己消失了"）。
    现在先探活：能连上就拒绝启动；连不上（残留的僵尸 socket）才删掉重建。
    """
    if hasattr(socketserver, "UnixStreamServer"):
        class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True
        _UnixServer.ctx = ctx

        if os.path.exists(socket_path) and _unix_socket_alive(socket_path):
            ctx.log("已有实例正在监听 %s，本进程不再重复启动（避免两个进程共写数据文件）"
                    % socket_path)
            ctx.single_instance_conflict = True
            return None

        try:
            os.unlink(socket_path)
        except OSError:
            pass
        server = _UnixServer(socket_path, AppHandler)
        try:
            os.chmod(socket_path, 0o666)
        except OSError:
            pass
        return server
    return None


def create_tcp_server(host, port, ctx):
    """端口直连模式 / 本机开发调试。"""
    class _TcpServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    _TcpServer.ctx = ctx
    return _TcpServer((host, int(port)), AppHandler)
