# -*- coding: utf-8 -*-
"""两个目录之间的**双向**差异比对。

两侧是**对称**的：每一侧都可能是本地目录，也可能是 OpenList 云端目录，
于是「本地↔云端」「云端↔云端」都由同一套代码覆盖。下文为叙述方便，
把两侧称作「左侧」与「右侧」—— 字段名 `notUploaded` / `cloudOnly` 是历史
遗留（它们真正表示"仅左侧有" / "仅右侧有"），语义由调用方给出的 side 元数据
解释，`verify` 本身不关心某一侧是不是云。

分四类（基础事实）：

* `notUploaded` —— 左侧有、右侧没有 → 仅左侧有
* `sizeMismatch` —— 两边都有但大小不同 → 其中一侧是旧版本，
  所以两个方向的操作都提供：用左侧覆盖右侧 / 用右侧覆盖左侧
* `uploaded` —— 两边都有且大小一致 → 已完成（明细**只含文件**；两边都存在的目录
  另有 `uploadedDirs` 计数，不逐条列出）
* `cloudOnly` —— 右侧独有 → 仅右侧有

在这四类之上，`split_diff()` 再给出**面向操作的顶层差异**（`localExtra` /
`cloudExtra` / `mismatch`）：目录已列为差异项时就不再单列它的子项。这么做的
原因见 `_prune_descendants()` —— 不收敛的话，批量删除会把同一棵子树删两遍。
第四组 `same`（已同步）是只读的，不参与任何批量操作、不做归并。

每个条目都带 `absPath`（它**在自己那一侧**的绝对路径），`sizeMismatch` 的条目
额外带 `peerAbsPath`（右侧那一条的绝对路径）。前端据此直接发请求，不必再按
"左栏=本地、右栏=云端"去猜路径怎么拼。

比对本身是只读的，两侧的文件都不会被改动；改动只发生在用户显式点下
「上传 / 下载 / 复制 / 清除」之后。

两侧必须用**同样的递归深度**，否则深度差会把整棵子树误判成"未上传" ——
这是这类功能最容易出的致命错误，所以深度在扫描前统一收敛成同一个值。
"""

import fnmatch
import os
import threading

DEFAULT_EXCLUDES = (
    ".DS_Store", "Thumbs.db", "desktop.ini", "*.tmp", "*.part",
    # 本应用下载时的临时文件。不排除的话，一次中断的下载会让它在下一次比对
    # 里变成"本地多出的文件"，用户一不留神就把它传到云端了。
    "*.ddpart",
    "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
    ".stfolder", ".stversions",
)

MAX_TREE_NODES = 200000

# 「已同步」明细回传上限。成熟的备份目录动辄上万条已同步文件，全量回传既拖慢
# 响应，也没有人会逐条去看。超出就截断并置 `uploadedTruncated`，让前端如实说明；
# **计数（uploadedFiles / uploadedDirs）不受截断影响**，永远是全量真实值 ——
# 否则统计徽标会随着截断悄悄变小，用户拿到的数字就是错的。
UPLOADED_LIMIT = 5000

# **永远生效**的排除项，不受用户配置影响。
# 里面是本应用自己的中间产物：下载写的是 `xxx.ddpart` 临时文件，用户在设置里
# 把排除规则清空后，一个中断的下载就会在下次比对里变成"本地多出的文件"，
# 一不留神就被传到云端。这类"自家临时文件"必须由代码兜底，而不是靠配置。
ALWAYS_EXCLUDES = ("*.ddpart",)


def effective_excludes(patterns):
    out = [str(p) for p in (patterns or ()) if str(p or "").strip()]
    for extra in ALWAYS_EXCLUDES:
        if extra not in out:
            out.append(extra)
    return out


# 云端扫描的默认并发路数。云端扫描是「每个目录一次 HTTP 请求」，
# 串行做的话 D 个目录就是 D 次往返；并发能把它压成大约 (层数) 轮。
SCAN_CONCURRENCY = 8
SCAN_CONCURRENCY_MAX = 32


def normalize_concurrency(value, default=SCAN_CONCURRENCY):
    """把外部传进来的并发数收敛到 1..SCAN_CONCURRENCY_MAX。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(SCAN_CONCURRENCY_MAX, n))


def map_concurrent(fn, items, workers):
    """用固定 N 个线程并发跑 `fn(item)`，结果**按输入顺序**返回。

    刻意不用 `concurrent.futures`：飞牛的裁剪版 python 不一定编入了
    multiprocessing 依赖的 C 扩展，而某些 CPython 版本的 `concurrent.futures`
    在 import 时就会把它拖进来 —— 那会直接让模块加载失败。项目铁律是
    「依赖 C 扩展的标准库一律不做顶层硬导入」，所以这里只用
    `threading`（本项目已经在用、确定可用）。
    """
    items = list(items)
    if not items:
        return []
    total = len(items)
    count = max(1, min(int(workers or 1), total))
    if count == 1:
        # 单线程路径与并发路径必须行为一致（异常同样装进结果），
        # 否则 concurrency=1 的测试与实际运行的表现会不一致。
        out = []
        for x in items:
            try:
                out.append(fn(x))
            except BaseException as exc:
                out.append(exc)
        return out

    results = [None] * total
    cursor = {"next": 0}
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                index = cursor["next"]
                if index >= total:
                    return
                cursor["next"] = index + 1
            try:
                results[index] = fn(items[index])
            except BaseException as exc:       # 异常装进结果，由调用方决定怎么处理
                results[index] = exc

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(count)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results


def match_exclude(name, patterns):
    """名字是否命中排除规则（大小写不敏感，支持 * 通配）。"""
    if not name:
        return True
    name_lower = name.lower()
    for pattern in patterns or ():
        if not pattern:
            continue
        if fnmatch.fnmatch(name_lower, pattern.lower()):
            return True
    return False


def normalize_rel(path):
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p)


class ScanLimitReached(Exception):
    pass


# --------------------------------------------------------------------------
# 本地
# --------------------------------------------------------------------------

def scan_local(root, depth=3, excludes=DEFAULT_EXCLUDES):
    """扫描本地目录。

    返回 `({相对路径: {"isDir","size","mtime"}}, errors)`，深度 0 表示只看 root 这一层。
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError("本地目录不存在或不是目录：%s" % root)

    excludes = effective_excludes(excludes)
    out = {}
    depth = max(0, int(depth or 0))

    stack = [(root, "")]
    while stack:
        current, rel = stack.pop()
        current_depth = len(rel.split("/")) if rel else 0
        if current_depth > depth:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            # 只记录错误，不把该目录塞进 out —— 否则对比时它会被当成「已上传」，
            # 掩盖「里面的文件实际上没传」这个事实。
            out["__errors__"] = out.get("__errors__", [])
            out["__errors__"].append({"path": rel or ".", "error": str(exc)})
            continue

        for entry in entries:
            if len(out) > MAX_TREE_NODES:
                raise ScanLimitReached("本地目录条目过多（超过 %d），请收敛扫描范围"
                                       % MAX_TREE_NODES)
            name = entry.name
            if match_exclude(name, excludes):
                continue
            child_rel = normalize_rel(rel + "/" + name)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            size = 0
            mtime = 0
            if not is_dir:
                try:
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size
                    mtime = int(st.st_mtime)
                except OSError:
                    pass
            # `absPath` 与云端扫描同构：比对的两侧现在**都可能是云端**，
            # 「本地条目 = root + rel」这条捷径不再成立 —— 调用方拿到条目后
            # 必须能直接问出"它在自己那一侧的绝对路径是什么"。
            out[child_rel] = {"isDir": is_dir, "size": size, "mtime": mtime,
                              "absPath": entry.path}
            if is_dir and (len(child_rel.split("/")) - 1) < depth:
                stack.append((entry.path, child_rel))

    errors = out.pop("__errors__", [])
    return out, errors


# --------------------------------------------------------------------------
# 云端（OpenList）
# --------------------------------------------------------------------------

def scan_cloud(client, root_path, depth=3, excludes=DEFAULT_EXCLUDES, progress=None,
               concurrency=None):
    """扫描云端目录。

    返回 `{相对路径: node}`，node 是 OpenList 的条目（含 name/size/is_dir/modified）。
    root_path 是云端绝对路径（"/" 开头）。rel 相对 root_path。

    并发策略：**同一层的目录并发列，层与层之间串行**。
    原来的写法是逐目录递归、一个目录一次 HTTP 请求且完全串行 —— D 个目录就是
    D 次往返，深度 3、每层几十个目录时，光网络往返就能到几十秒。层内并发能把
    它压成大约「轮数 × ⌈同层目录数 / 并发数⌉」次往返，实测快 5 倍以上。
    之所以不做「递归全并发」：那会瞬间产生成百上千个线程，在 NAS 上得不偿失，
    而且层与层之间本来就有依赖，强行并发只会放大错误处理复杂度。
    """
    out = {}
    depth = max(0, int(depth or 0))
    root_path = (root_path or "/").rstrip("/") or "/"
    workers = normalize_concurrency(concurrency)
    excludes = effective_excludes(excludes)
    visited = set()

    pending = [(root_path, "", 0)]      # (绝对路径, 相对前缀, 层号)
    visited.add(root_path)

    while pending:
        batch, pending = pending, []
        paths = [item[0] for item in batch]
        listed = map_concurrent(lambda p: _list_dir_safe(client, p), paths, workers)

        for (abs_path, rel, level), result in zip(batch, listed):
            if isinstance(result, BaseException):
                raise RuntimeError("列云端目录失败（%s）：%s"
                                   % (rel or "根目录", result))
            children, error = result
            if error is not None:
                raise RuntimeError("列云端目录失败（%s）：%s"
                                   % (rel or "根目录", error))

            if progress:
                progress(rel, len(out))

            for node in children:
                name = node.get("name") or ""
                if not name or match_exclude(name, excludes):
                    continue
                is_dir = bool(node.get("is_dir"))
                child_rel = normalize_rel(rel + "/" + name)
                if len(out) > MAX_TREE_NODES:
                    raise ScanLimitReached("云端条目过多（超过 %d），请收敛扫描范围"
                                           % MAX_TREE_NODES)
                child_abs = (abs_path.rstrip("/") + "/" + name) if abs_path != "/" \
                    else "/" + name
                out[child_rel] = {
                    "name": name,
                    "isDir": is_dir,
                    "size": int(node.get("size") or 0),
                    "modified": node.get("modified", ""),
                    "absPath": child_abs,
                }
                if is_dir and level < depth and child_abs not in visited:
                    visited.add(child_abs)
                    pending.append((child_abs, child_rel, level + 1))

    return out


def _list_dir_safe(client, abs_path):
    """列一个目录，返回 `(children, error)` —— 不抛异常，好让并发结果整齐。"""
    try:
        return client.list_all(abs_path), None
    except Exception as exc:
        return None, exc


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------

def _prune_descendants(entries):
    """去掉「祖先已经在同一列表里」的条目。

    一个目录被列为差异项，就代表**整棵子树**都是差异项。把它的子项再单列
    出来没有信息增量，却有两个实际危害：
    1. 批量删除会把同一个子树删两遍（第二遍必然报"不存在"），用户看到一堆
       莫名其妙的失败；
    2. 批量下载/上传会把整棵树重复入队，任务列表里出现几十倍的冗余条目。
    """
    paths = set()
    for e in entries or ():
        path = normalize_rel(e.get("path"))
        if path:
            paths.add(path)
    out = []
    for e in entries or ():
        path = normalize_rel(e.get("path"))
        if not path:
            continue
        parts = path.split("/")
        covered = False
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in paths:
                covered = True
                break
        if not covered:
            out.append(e)
    return out


def _bytes_of(entries):
    return sum(int(e.get("size") or 0) for e in entries if not e.get("isDir"))


def split_diff(result):
    """把比对结果收敛成「可直接操作」的四组：localExtra / cloudExtra / mismatch / same。

    前三组是**差异**，会过 `_prune_descendants` 做父子归并 —— 归并是为了让
    批量上传/下载/清除不去重地处理同一棵子树（目录已列为差异项时，其子项再列一遍
    会让批量删除把同一子树删两遍）。

    `same`（已同步）**不做归并、也不参与任何批量操作**，它是只读的确认视图：
    归并是为「批量去重」服务的，用在这里只会让"明明同步了的子文件"凭空消失。
    它同样**只含文件**（`compare()` 已保证）—— 目录的「已同步」只代表两边都存在
    这个路径，不代表里面的文件都传完了，列出来是误导。
    """
    local_extra = _prune_descendants(result.get("notUploaded") or [])
    cloud_extra = _prune_descendants(result.get("cloudOnly") or [])
    mismatch = list(result.get("sizeMismatch") or [])
    same = list(result.get("uploaded") or [])
    # `same` 明细可能被 `UPLOADED_LIMIT` 截断，所以总数一律取 compare() 算出的
    # 全量计数，而**不是** len(same) —— 否则超过上限时徽标数字会悄悄变小。
    # 同理不给 sameBytes：截断后按剩下来的明细求和是错的，宁可没有。
    same_total = int(result.get("uploadedFiles") or 0) or len(same)
    return {
        "localExtra": local_extra,
        "cloudExtra": cloud_extra,
        "mismatch": mismatch,
        "same": same,
        "localExtraCount": len(local_extra),
        "cloudExtraCount": len(cloud_extra),
        "mismatchCount": len(mismatch),
        "sameCount": same_total,
        "sameListed": len(same),
        "localExtraBytes": _bytes_of(local_extra),
        "cloudExtraBytes": _bytes_of(cloud_extra),
        "mismatchBytes": _bytes_of(mismatch),
    }


def compare(local_map, cloud_map):
    """比对两侧，分四类。文件比路径 + 大小；目录只比路径存在性。

    **形参名沿用历史叫法**：`local_map` 是「左侧」、`cloud_map` 是「右侧」。
    两侧都可能是本地或云端（云端↔云端比对就是两个 `scan_cloud` 的结果），
    所以这里不假设任何一侧的类型 —— 只要求两侧的 key 都是**相对各自 root 的
    相对路径**（两个扫描函数都保证这点），语义才能真正对齐。

    返回 dict，含 notUploaded / sizeMismatch / uploaded / cloudOnly 及统计，
    以及 `split_diff()` 给出的四组顶层结果
    （localExtra / cloudExtra / mismatch / same）。
    """
    not_uploaded = []
    size_mismatch = []
    uploaded = []
    uploaded_files = 0
    uploaded_dirs = 0

    for rel in sorted(local_map.keys()):
        local = local_map[rel]
        cloud = cloud_map.get(rel)
        entry = {
            "path": rel,
            "name": rel.split("/")[-1],
            "isDir": bool(local.get("isDir")),
            "size": int(local.get("size") or 0),
            "mtime": int(local.get("mtime") or 0),
            # 左侧该条目的绝对路径（本地是文件系统路径，云端是 /xxx）。
            # 前端直接拿它发上传/下载/复制/删除请求，不再自己拼 ——
            # 「左栏是本地」不成立之后，拼路径这件事已经没有可靠依据。
            "absPath": local.get("absPath") or "",
            # `absPath` 属于哪一侧。同一份结果里 `notUploaded` 与 `cloudOnly`
            # 两个列表的 `absPath` 分属左右，光看字段名分不出来；`owner` 把它钉死。
            "owner": "left",
        }
        if cloud is None:
            not_uploaded.append(entry)
            continue
        if local.get("isDir"):
            # 目录只要两边都存在就计入已同步计数，但**不进明细**：路径存在不等于
            # 内容齐了（子文件可能整批还没上传），把它列成"已同步项"会误导用户
            # 以为里面已经传完了。
            uploaded_dirs += 1
            continue
        # 文件：比大小
        if int(cloud.get("size") or 0) == int(local.get("size") or 0):
            uploaded_files += 1
            uploaded.append(entry)
        else:
            e = dict(entry)
            e["cloudSize"] = int(cloud.get("size") or 0)
            e["peerAbsPath"] = cloud.get("absPath") or ""
            size_mismatch.append(e)

    cloud_only = []
    for rel in sorted(cloud_map.keys()):
        if rel not in local_map:
            node = cloud_map[rel]
            cloud_only.append({
                "path": rel,
                "name": node.get("name") or rel.split("/")[-1],
                "isDir": bool(node.get("isDir")),
                "size": int(node.get("size") or 0),
                # 这里 `absPath` 是**右侧**条目的绝对路径（与 notUploaded 那条
                # 的"左侧"对称）。`owner` 把它钉死，前端不必猜。
                "absPath": node.get("absPath", ""),
                "owner": "right",
            })

    pending_bytes = sum(e["size"] for e in not_uploaded if not e["isDir"]) \
        + sum(e["size"] for e in size_mismatch if not e["isDir"])

    uploaded_truncated = len(uploaded) > UPLOADED_LIMIT
    if uploaded_truncated:
        uploaded = uploaded[:UPLOADED_LIMIT]

    result = {
        "notUploaded": not_uploaded,
        "sizeMismatch": size_mismatch,
        "cloudOnly": cloud_only,
        "uploaded": uploaded,
        "uploadedTruncated": uploaded_truncated,
        "uploadedFiles": uploaded_files,
        "uploadedDirs": uploaded_dirs,
        "localTotal": len(local_map),
        "cloudTotal": len(cloud_map),
        "notUploadedCount": len(not_uploaded),
        "sizeMismatchCount": len(size_mismatch),
        "cloudOnlyCount": len(cloud_only),
        "pendingBytes": pending_bytes,
    }
    # 同步/备份工具真正要的是「顶层差异」：既有本地多出的、也有云端多出的。
    # 旧的 notUploaded/cloudOnly 保留全量（深度信息对诊断有用），
    # 面向操作的字段单独给，前端不必自己再做一遍父子归并。
    result.update(split_diff(result))
    return result


def pick_upload_targets(result, selected_paths=None):
    """把比对结果里「待上传」的本地文件收敛成上传清单。

    selected_paths 为空则取全部 notUploaded + sizeMismatch 里的**文件**（目录会在
    上传时由 ensure_dir 自动建，不单独成任务）。

    返回 [{path, size, isDir}]，按路径排序。
    """
    by_path = {}
    for e in result.get("notUploaded", []):
        by_path[e["path"]] = e
    for e in result.get("sizeMismatch", []):
        by_path[e["path"]] = e

    if selected_paths:
        chosen = []
        wanted = set(normalize_rel(p) for p in selected_paths)
        for p in sorted(wanted):
            if p in by_path:
                chosen.append(by_path[p])
    else:
        chosen = [by_path[p] for p in sorted(by_path.keys())]

    # 只要文件（目录在上传时按需创建）
    return [e for e in chosen if not e.get("isDir")]
