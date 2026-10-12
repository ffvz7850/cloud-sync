# -*- coding: utf-8 -*-
"""目录绑定功能测试：BindingStore 业务逻辑 + HTTP 路由。

覆盖：
* CRUD 与输入校验
* 增量扫描：missing / mismatch / uploaded、多目标、禁用目标、云端扫描失败
* 上传：单文件 / 批量 / 全部 / 指定目标 / 不重复入队
* 持久化：重新加载绑定仍在
* 队列状态同步
"""
import json
import os
import threading
import time
import unittest

import support

import config
import http_api
import openlist
import upload_queue
import bindings as bindings_mod


# --------------------------------------------------------------------------
# 假客户端：云端树按路径配置
# --------------------------------------------------------------------------

class BindingFakeClient(object):
    """可配置云端目录树的假客户端。

    tree: {absPath: [{"name","is_dir","size"}, ...]}
    scan_errors: 对某路径抛异常（模拟云端目录不可读）
    stats: {absPath: {"name","is_dir","size"} 或 None}，供 stat() 使用
    copy_tasks: copy 成功后服务端返回的任务 id 列表；
                None 表示"同存储同步复制、不建任务"（返回空 tasks）
    tasks_supported: 是否实现 list_tasks（False → list_tasks 返回 None，
                     模拟任务接口查询失败，此时不得误判成功）
    on_copy: 每次 copy 真正发出**之前**的回调（src_dir, dst_dir, names）。
             提交进度的回归测试靠它拿到「提交正在进行中」的瞬时快照，
             不依赖 sleep 时序，并发下也稳定。
    copy_delay: 每次 copy 前的固定延时（模拟慢网络；默认 0）
    """

    def __init__(self, tree=None, scan_errors=None, stats=None,
                 copy_tasks=None, tasks_supported=False,
                 on_copy=None, copy_delay=0.0):
        self.tree = tree or {}
        self.scan_errors = scan_errors or {}
        self.stats = stats or {}
        self.copy_tasks = copy_tasks
        self.tasks_supported = tasks_supported
        self.on_copy = on_copy
        self.copy_delay = copy_delay
        self.task_list = {"undone": [], "done": []}
        self.copies = []          # [(src_dir, dst_dir, names, kwargs)]
        self.copy_error = None    # 非 None 时 copy 抛该异常
        self.list_calls = []      # 每次 list_all 的路径（性能回归用）
        self.stat_calls = []      # 每次 stat 的路径（性能回归用）
        self.mkdir_calls = []     # 每次 mkdir 的路径（并发去重回归用）
        self.task_fetches = 0     # list_tasks 被真正调用了几次（缓存回归用）

    def list_all(self, path, meta=None):
        self.list_calls.append(path)
        if path in self.scan_errors:
            raise openlist.OpenlistError(self.scan_errors[path])
        out = [dict(n) for n in self.tree.get(path, [])]
        # 显式配置的 `stats` 也要反映到目录列表里：`stat()` 与「按父目录批量列」
        # 查的是同一个云端，两条路径的数据源必须一致，否则测试会给出错误结论
        # （真实 OpenList 上它们本来就是一回事）。
        base = (path or "").rstrip("/")
        seen = set(n.get("name") for n in out)
        for full, node in self.stats.items():
            if node is None:
                continue
            parent, _, name = (full or "").rstrip("/").rpartition("/")
            if (parent or "/") == (base or "/") and name not in seen:
                out.append(dict(node))
                seen.add(name)
        return out

    def list_dir(self, path, page=1, per_page=1000):
        content = self.list_all(path)
        return {"content": content, "total": len(content),
                "write": True, "provider": "fake"}

    def ensure_dir(self, path, cache=None):
        """与真实实现**同构**：逐级从浅到深、每级各自加锁；带共享缓存。

        刻意不写成空实现。空实现测不出「一次提交到底发了多少次请求」，
        而那正是「提交慢」的主因 —— 实测提交 30 个子目录 / 120 个文件时发了
        156~177 次请求（其中 54 次 stat + 18 次 mkdir 是纯重复）。
        """
        path = (path or "/").strip()
        if not path or path == "/":
            return
        cache = cache if cache is not None else openlist.DirCache()
        if cache.known(path):
            return
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            if cache.known(current):
                continue
            with cache.lock_for(current):
                if cache.known(current):
                    continue
                if self._is_dir(current):
                    cache.mark(current)
                    continue
                try:
                    self.mkdir(current)
                except openlist.OpenlistError as exc:
                    node = self.stat(current)
                    if node is None:
                        raise openlist.OpenlistError(
                            "创建云端目录失败（%s）：%s" % (current, exc))
                    if not node.get("is_dir"):
                        raise openlist.OpenlistError(
                            "云端路径已存在且不是目录：%s" % current)
                cache.mark(current)

    def _is_dir(self, path):
        node = self.stat(path)
        if node is None:
            return False
        if not node.get("is_dir"):
            raise openlist.OpenlistError("云端路径已存在且不是目录：%s" % path)
        return True

    def mkdir(self, path):
        self.mkdir_calls.append(path)
        parent, _, name = (path or "").rstrip("/").rpartition("/")
        self.tree.setdefault(parent or "/", []).append(
            {"name": name, "is_dir": True, "size": 0})
        return {}

    def stat(self, path):
        """显式配置优先；否则按绝对路径从云端树推导，模拟真实的单文件查询。"""
        self.stat_calls.append(path)
        if path in self.stats:
            return self.stats[path]
        parent, _, name = (path or "").rstrip("/").rpartition("/")
        for node in self.tree.get(parent or "/", []):
            if node.get("name") == name:
                return dict(node)
        return None

    def list_tasks(self, task_type="copy"):
        self.task_fetches += 1
        if not self.tasks_supported:
            return None
        return self.task_list

    def copy(self, src_dir, dst_dir, names, **kw):
        # 回调在真正提交之前触发：进度回归测试靠它采样「提交进行中」的快照。
        if self.on_copy is not None:
            self.on_copy(src_dir, dst_dir, list(names))
        if self.copy_delay:
            time.sleep(self.copy_delay)
        # 记录 copy 调用，模拟服务端复制成功
        self.copies.append((src_dir, dst_dir, list(names), dict(kw)))
        if self.copy_error is not None:
            raise self.copy_error
        if self.copy_tasks is None:
            return {"tasks": []}
        return {"tasks": [{"id": tid} for tid in self.copy_tasks[:len(names)]]}


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

def make_ctx(tree=None, scan_errors=None):
    root = support.temp_data_dir()
    os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = root
    ctx = http_api.AppContext(config.APP_NAME)
    ctx._store = openlist.CredentialStore(os.path.join(root, "cred.json"))
    ctx._client = BindingFakeClient(tree, scan_errors)
    ctx._queue = upload_queue.UploadQueue(
        ctx._client, path=os.path.join(root, "q.json"))
    ctx._sandbox = root
    return ctx


def make_store(ctx, tree=None, scan_errors=None):
    """直接构造 BindingStore（不走 ctx.bindings()，可控性更强）。"""
    path = os.path.join(ctx._sandbox, "bindings.json")
    return bindings_mod.BindingStore(
        ctx._client, path=path, queue=ctx._queue)


def write_local(base, files):
    """files: {相对路径: 内容(bytes/str)}。"""
    for rel, content in files.items():
        full = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)


def jcall(ctx, method, path, body=None):
    status, headers, raw = support.call_handler(
        http_api.AppHandler, ctx, method, path, body=body)
    return status, support.json_body(raw)


# --------------------------------------------------------------------------
# 云端树构造辅助
# --------------------------------------------------------------------------

def cloud_tree(root, files):
    """从 {relPath: size} 构造 list_all 需要的目录树。

    会在父目录的条目列表里补上子目录节点（is_dir=True），
    否则递归扫描看不到子目录。
    """
    tree = {}
    root = root.rstrip("/")
    for rel, size in files.items():
        parts = rel.split("/")
        # 逐级建目录，并在父目录里登记子目录节点
        for i in range(len(parts)):
            if i == 0:
                dir_path = root
                tree.setdefault(dir_path, [])
            else:
                parent_path = root if i == 1 else root + "/" + "/".join(parts[:i-1])
                dir_path = root + "/" + "/".join(parts[:i])
                tree.setdefault(dir_path, [])
                # 在父目录里登记子目录
                parent_list = tree.setdefault(parent_path, [])
                child_name = parts[i-1]
                if not any(n["name"] == child_name and n.get("is_dir")
                           for n in parent_list):
                    parent_list.append({
                        "name": child_name, "is_dir": True,
                        "size": 0, "modified": "",
                    })
        # 把文件挂到直接父目录
        parent = root if len(parts) == 1 else root + "/" + "/".join(parts[:-1])
        file_list = tree.setdefault(parent, [])
        if not any(n["name"] == parts[-1] for n in file_list):
            file_list.append({
                "name": parts[-1], "is_dir": False, "size": size,
                "modified": "",
            })
    return tree


# --------------------------------------------------------------------------
# CRUD 测试
# --------------------------------------------------------------------------

class CrudTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.local = os.path.join(self.ctx._sandbox, "docs")
        os.makedirs(self.local)
        write_local(self.local, {"a.txt": "hello"})
        self.store = make_store(self.ctx)

    def test_create_success(self):
        b = self.store.create_binding({
            "name": "文档备份",
            "localPath": self.local,
            "cloudTargets": [{"path": "/backup"}],
        })
        self.assertTrue(b["id"].startswith("bd_"))
        self.assertEqual(b["name"], "文档备份")
        self.assertEqual(len(b["cloudTargets"]), 1)
        self.assertEqual(b["cloudTargets"][0]["path"], "/backup")
        self.assertTrue(b["cloudTargets"][0]["enabled"])

    def test_create_default_name(self):
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        self.assertEqual(b["name"], "docs")

    def test_create_missing_local(self):
        with self.assertRaises(ValueError):
            self.store.create_binding({
                "cloudTargets": ["/backup"],
            })

    def test_create_local_path_is_openlist_string(self):
        # localPath 现在是 OpenList 路径字符串，不做本机 isdir 检查
        b = self.store.create_binding({
            "localPath": "/some/openlist/path",
            "cloudTargets": ["/backup"],
        })
        self.assertEqual(b["localPath"], "/some/openlist/path")

    def test_create_no_targets(self):
        with self.assertRaises(ValueError):
            self.store.create_binding({
                "localPath": self.local,
                "cloudTargets": [],
            })

    def test_create_duplicate_targets_dedup(self):
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup", "/backup/"],
        })
        self.assertEqual(len(b["cloudTargets"]), 1)

    def test_update(self):
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        updated = self.store.update_binding(b["id"], {"name": "新名字"})
        self.assertEqual(updated["name"], "新名字")

    def test_update_not_found(self):
        self.assertIsNone(self.store.update_binding("nope", {"name": "x"}))

    def test_delete(self):
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        self.assertTrue(self.store.delete_binding(b["id"]))
        self.assertEqual(self.store.list_bindings(), [])

    def test_delete_not_found(self):
        self.assertFalse(self.store.delete_binding("nope"))

    def test_persistence_reload(self):
        self.store.create_binding({
            "name": "持久绑定",
            "localPath": self.local,
            "cloudTargets": ["/backup", "/mirror"],
        })
        # 重新加载
        store2 = make_store(self.ctx)
        bindings = store2.list_bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["name"], "持久绑定")
        self.assertEqual(len(bindings[0]["cloudTargets"]), 2)


# --------------------------------------------------------------------------
# 增量扫描测试
# --------------------------------------------------------------------------

class ScanTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.local = "/source"
        # OpenList 源目录 /source 下有 a.txt(100) b.txt(200) sub/c.txt(300)
        self.local_tree = cloud_tree("/source", {
            "a.txt": 100, "b.txt": 200, "sub/c.txt": 300,
        })
        self.store = make_store(self.ctx)

    def _create(self, cloud_tree_data, targets=None, depth=3):
        if targets is None:
            targets = ["/backup"]
        # 合并本地 tree 和云端 tree
        merged = dict(self.local_tree)
        merged.update(cloud_tree_data)
        client = BindingFakeClient(merged)
        self.store = bindings_mod.BindingStore(
            client, path=os.path.join(self.ctx._sandbox, "b.json"),
            queue=self.ctx._queue)
        return self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": targets,
            "scanDepth": depth,
        })

    def test_all_missing(self):
        b = self._create({})  # 云端空
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        rels = sorted(f["relPath"] for f in result["files"])
        self.assertEqual(rels, ["a.txt", "b.txt", "sub/c.txt"])
        # 每个目标状态都是 missing
        for f in result["files"]:
            for ts in f["targets"].values():
                self.assertEqual(ts["status"], "missing")

    def test_all_uploaded(self):
        tree = cloud_tree("/backup", {
            "a.txt": 100, "b.txt": 200, "sub/c.txt": 300,
        })
        b = self._create(tree)
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["scanStatus"], "done")

    def test_partial_missing(self):
        # 云端只有 a.txt
        tree = cloud_tree("/backup", {"a.txt": 100})
        b = self._create(tree)
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        rels = sorted(f["relPath"] for f in result["files"])
        self.assertEqual(rels, ["b.txt", "sub/c.txt"])

    def test_size_mismatch(self):
        # 云端 a.txt 大小不对
        tree = cloud_tree("/backup", {"a.txt": 99})
        b = self._create(tree)
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        a = next(f for f in result["files"] if f["relPath"] == "a.txt")
        ts = list(a["targets"].values())[0]
        self.assertEqual(ts["status"], "mismatch")
        self.assertEqual(ts["cloudSize"], 99)

    def test_multiple_targets_mixed(self):
        # 目标1 /backup 有 a.txt；目标2 /mirror 空
        tree1 = cloud_tree("/backup", {"a.txt": 100})
        tree2 = {}
        # 合并两棵树
        combined = dict(tree1)
        combined.update(tree2)
        merged = dict(self.local_tree)
        merged.update(combined)
        client = BindingFakeClient(merged)
        self.store = bindings_mod.BindingStore(
            client, path=os.path.join(self.ctx._sandbox, "b.json"),
            queue=self.ctx._queue)
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup", "/mirror"],
        })
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])

        # a.txt 在 backup 已上传，在 mirror 缺失 → 应出现在 files
        a = next(f for f in result["files"] if f["relPath"] == "a.txt")
        statuses = {t["path"]: s for t in b["cloudTargets"]
                    for s in [a["targets"][t["id"]]]}
        # /backup 上是 uploaded（不在 files 的原因？不，因为 mirror 缺失所以进了 files）
        backup_tid = next(t["id"] for t in b["cloudTargets"]
                          if t["path"] == "/backup")
        mirror_tid = next(t["id"] for t in b["cloudTargets"]
                          if t["path"] == "/mirror")
        self.assertEqual(a["targets"][backup_tid]["status"], "uploaded")
        self.assertEqual(a["targets"][mirror_tid]["status"], "missing")

    def test_disabled_target_skipped(self):
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": [
                {"path": "/backup", "enabled": True},
                {"path": "/mirror", "enabled": False},
            ],
        })
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        # 禁用的目标不应该出现在 targets 里
        for f in result["files"]:
            paths = [t["path"] for t in b["cloudTargets"]
                     if t["id"] in f["targets"]]
            self.assertNotIn("/mirror", paths)

    def test_depth_limits_scan(self):
        # depth=0 只扫当前层
        b = self._create({}, depth=0)
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        rels = [f["relPath"] for f in result["files"]]
        self.assertIn("a.txt", rels)
        self.assertNotIn("sub/c.txt", rels)

    def test_cloud_scan_error_marks_failed(self):
        client = BindingFakeClient({}, scan_errors={"/backup": "存储不可用"})
        self.store = bindings_mod.BindingStore(
            client, path=os.path.join(self.ctx._sandbox, "b.json"),
            queue=self.ctx._queue)
        b = self.store.create_binding({
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        self.store.start_scan(b["id"], background=False)
        result = self.store.get_binding(b["id"])
        # 扫描仍完成（单个目标失败不阻塞），文件状态为 failed
        for f in result["files"]:
            ts = list(f["targets"].values())[0]
            self.assertEqual(ts["status"], "failed")
            self.assertIn("存储不可用", ts["error"])


# --------------------------------------------------------------------------
# 上传测试
# --------------------------------------------------------------------------

class UploadTest(unittest.TestCase):
    def setUp(self):
        tree = cloud_tree("/source", {"a.txt": 100, "b.txt": 200, "sub/c.txt": 300})
        self.ctx = make_ctx(tree=tree)
        self.ctx._client.tree = tree
        self.store = make_store(self.ctx, tree=tree)
        self.b = self.store.create_binding({
            "localPath": "/source",
            "cloudTargets": ["/backup"],
        })
        self.store.start_scan(self.b["id"], background=False)

    def test_upload_single_file(self):
        count = self.store.upload_files(
            self.b["id"], rel_paths=["a.txt"])
        self.assertEqual(count, 1)
        self.assertEqual(len(self.ctx._client.copies), 1)
        src_dir, dst_dir, names, _kw = self.ctx._client.copies[0]
        self.assertEqual(src_dir, "/source")
        self.assertEqual(dst_dir, "/backup")
        self.assertEqual(names, ["a.txt"])
        result = self.store.get_binding(self.b["id"])
        a = next(f for f in result["files"] if f["relPath"] == "a.txt")
        self.assertEqual(list(a["targets"].values())[0]["status"], "queued")

    def test_upload_multiple(self):
        count = self.store.upload_files(
            self.b["id"], rel_paths=["a.txt", "b.txt"])
        self.assertEqual(count, 2)
        self.assertEqual(len(self.ctx._client.copies), 1)

    def test_upload_all(self):
        count = self.store.upload_files(self.b["id"], upload_all=True)
        self.assertEqual(count, 3)

    def test_upload_to_specific_target(self):
        b = self.store.create_binding({
            "localPath": "/source",
            "cloudTargets": ["/backup", "/mirror"],
        })
        self.store.start_scan(b["id"], background=False)
        mirror_tid = next(t["id"] for t in b["cloudTargets"]
                          if t["path"] == "/mirror")
        count = self.store.upload_files(
            b["id"], rel_paths=["a.txt"], target_ids=[mirror_tid])
        self.assertEqual(count, 1)
        self.assertEqual(self.ctx._client.copies[-1][1], "/mirror")

    def test_no_duplicate_upload_queued(self):
        # 第一次上传
        self.store.upload_files(self.b["id"], rel_paths=["a.txt"])
        # 再次上传同一文件（已在队列中）→ 不重复
        count = self.store.upload_files(
            self.b["id"], rel_paths=["a.txt"])
        self.assertEqual(count, 0)

    def test_upload_subdir_file_groups_by_dir(self):
        count = self.store.upload_files(
            self.b["id"], rel_paths=["sub/c.txt"])
        self.assertEqual(count, 1)
        src_dir, dst_dir, names, _kw = self.ctx._client.copies[0]
        self.assertEqual(src_dir, "/source/sub")
        self.assertEqual(dst_dir, "/backup/sub")
        self.assertEqual(names, ["c.txt"])


# --------------------------------------------------------------------------
# 多目标上传测试
# --------------------------------------------------------------------------

class MultiTargetUploadTest(unittest.TestCase):
    def setUp(self):
        tree = cloud_tree("/source", {"a.txt": 100})
        self.ctx = make_ctx(tree=tree)
        self.ctx._client.tree = tree
        self.store = make_store(self.ctx, tree=tree)
        self.b = self.store.create_binding({
            "localPath": "/source",
            "cloudTargets": ["/backup", "/mirror"],
        })
        self.store.start_scan(self.b["id"], background=False)

    def test_upload_all_targets(self):
        count = self.store.upload_files(self.b["id"], upload_all=True)
        self.assertEqual(count, 2)
        # 两次 copy（每个目标一次）
        self.assertEqual(len(self.ctx._client.copies), 2)

    def test_binding_file_tracks_both_targets(self):
        self.store.upload_files(self.b["id"], upload_all=True)
        result = self.store.get_binding(self.b["id"])
        a = result["files"][0]
        self.assertEqual(len(a["targets"]), 2)
        for ts in a["targets"].values():
            self.assertEqual(ts["status"], "queued")


# --------------------------------------------------------------------------
# HTTP 路由测试
# --------------------------------------------------------------------------

class BindingRouteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.local = "/source"
        tree = cloud_tree("/source", {"a.txt": 100})
        self.ctx._client.tree = tree

    def test_list_empty(self):
        status, data = jcall(self.ctx, "GET", "/api/bindings")
        self.assertEqual(status, 200)
        self.assertEqual(data["bindings"], [])

    def test_create_via_http(self):
        status, data = jcall(self.ctx, "POST", "/api/bindings/create", {
            "name": "测试",
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["binding"]["name"], "测试")

    def test_create_invalid_via_http(self):
        status, data = jcall(self.ctx, "POST", "/api/bindings/create", {
            "cloudTargets": [],
        })
        self.assertEqual(status, 400)

    def test_detail_not_found(self):
        status, data = jcall(
            self.ctx, "GET", "/api/bindings/detail?id=nope")
        self.assertEqual(status, 404)

    def test_full_workflow_via_http(self):
        # 创建
        _, data = jcall(self.ctx, "POST", "/api/bindings/create", {
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        bid = data["binding"]["id"]

        # 同步扫描（后台线程在测试中不好控制，直接调 store）
        store = self.ctx.bindings()
        store.start_scan(bid, background=False)

        # 详情应包含缺失文件
        status, data = jcall(
            self.ctx, "GET", "/api/bindings/detail?id=" + bid)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["binding"]["files"]), 1)

        # 上传
        status, data = jcall(self.ctx, "POST", "/api/bindings/upload", {
            "id": bid, "all": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["queued"], 1)

        # 删除
        status, data = jcall(self.ctx, "POST", "/api/bindings/delete", {
            "id": bid,
        })
        self.assertEqual(status, 200)

    def test_scan_all_route(self):
        jcall(self.ctx, "POST", "/api/bindings/create", {
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        status, data = jcall(
            self.ctx, "POST", "/api/bindings/scan-all", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["started"], 1)

    def test_update_via_http(self):
        _, data = jcall(self.ctx, "POST", "/api/bindings/create", {
            "localPath": self.local,
            "cloudTargets": ["/backup"],
        })
        bid = data["binding"]["id"]
        status, data = jcall(self.ctx, "POST", "/api/bindings/update", {
            "id": bid, "name": "新名称",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["binding"]["name"], "新名称")


# --------------------------------------------------------------------------
# 自动上传测试
# --------------------------------------------------------------------------

class AutoUploadTest(unittest.TestCase):
    def test_auto_upload_calls_copy_after_scan(self):
        tree = cloud_tree("/source", {"a.txt": 100, "b.txt": 200})
        ctx = make_ctx(tree=tree)
        ctx._client.tree = tree
        store = bindings_mod.BindingStore(
            ctx._client, path=os.path.join(ctx._sandbox, "b.json"),
            queue=ctx._queue)
        b = store.create_binding({
            "localPath": "/source",
            "cloudTargets": ["/backup"],
            "autoUpload": True,
        })
        store.start_scan(b["id"], background=False)
        # 扫描后自动 copy（同目录合并成一次）
        self.assertEqual(len(ctx._client.copies), 1)
        self.assertEqual(ctx._client.copies[0][2], ["a.txt", "b.txt"])


# --------------------------------------------------------------------------
# 复制结果判定（回归：消灭"查不到 = 成功"的误报）
# --------------------------------------------------------------------------

def ready_ctx(files=None, copy_tasks=None, tasks_supported=False,
              target_cloud=None, extra=None):
    """建一个扫描完毕、可以直接上传的 ctx/store/binding。"""
    files = files or {"a.txt": 100, "b.txt": 200}
    tree = cloud_tree("/source", files)
    if target_cloud:
        tree.update(cloud_tree("/backup", target_cloud))
    if extra:
        tree.update(extra)
    ctx = make_ctx(tree=tree)
    ctx._client.copy_tasks = copy_tasks
    ctx._client.tasks_supported = tasks_supported
    store = make_store(ctx)
    b = store.create_binding({
        "localPath": "/source",
        "cloudTargets": ["/backup"],
    })
    store.start_scan(b["id"], background=False)
    return ctx, store, b["id"]


def target_state(store, bid, rel):
    detail = store.get_binding(bid)
    f = next(x for x in detail["files"] if x["relPath"] == rel)
    return list(f["targets"].values())[0]


class ConflictFlagTest(unittest.TestCase):
    """冲突策略必须真的传给服务端，而不是只写进历史。"""

    def _upload_with_policy(self, policy):
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        store.update_binding(bid, {"conflictPolicy": policy})
        store.upload_files(bid, rel_paths=["a.txt"])
        return ctx

    def test_skip_passes_skip_existing(self):
        ctx = self._upload_with_policy("skip")
        self.assertEqual(ctx._client.copies[0][3], {"skip_existing": True})

    def test_overwrite_passes_overwrite(self):
        ctx = self._upload_with_policy("overwrite")
        self.assertEqual(ctx._client.copies[0][3], {"overwrite": True})

    def test_abort_passes_no_flags(self):
        ctx = self._upload_with_policy("abort")
        self.assertEqual(ctx._client.copies[0][3], {})


class TaskIdClaimTest(unittest.TestCase):
    """任务 id 只在服务端为每个名字都建了任务时才认领。"""

    def test_claimed_when_count_matches(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1", "t2"])
        store.upload_files(bid, upload_all=True)
        self.assertEqual(target_state(store, bid, "a.txt")["taskId"], "t1")
        self.assertEqual(target_state(store, bid, "b.txt")["taskId"], "t2")

    def test_not_claimed_when_count_mismatch(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"])
        store.upload_files(bid, upload_all=True)
        self.assertEqual(target_state(store, bid, "a.txt")["taskId"], "")
        self.assertEqual(target_state(store, bid, "b.txt")["taskId"], "")

    def test_no_task_id_for_same_storage_copy(self):
        ctx, store, bid = ready_ctx(copy_tasks=None)
        store.upload_files(bid, upload_all=True)
        self.assertEqual(target_state(store, bid, "a.txt")["taskId"], "")


class VerdictTest(unittest.TestCase):
    """任务/云端证据不足时绝不放行成 uploaded。"""

    def test_task_query_unavailable_keeps_queued(self):
        # 任务接口查不到，云端也 stat 不到 → 保持原状态，不得判成功
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=False)
        store.upload_files(bid, rel_paths=["a.txt"])
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "queued")

    def test_task_id_not_found_keeps_in_flight_within_grace(self):
        """任务列表能查到，但认领的任务 id 不在里面。

        OpenList 完成任务后会清理任务记录，跨存储复制返回的 `data["tasks"]` 里的 id
        与 `/api/task/copy/*` 里的 id 也未必同源 —— 所以「id 找不到」并不能证明这次
        复制没成。刚提交就判缺失，会让任务立刻从传输列表消失、卡片又变回「缺失」，
        看起来像点了没反应（而云端其实正在复制），还会把成功的复制判成缺失、
        导致用户重传后重复上传。宽限期过后仍拿不到文件才下结论。
        """
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "queued")

    def test_task_gone_marks_missing_after_timeout(self):
        # 过了宽限期仍是「任务查不到 + 云端没有文件」→ 判缺失让用户重传，
        # 不能永远停在排队中。
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        with store._lock:
            b = store._find(bid)
            for f in b["files"]:
                for ts in (f.get("targets") or {}).values():
                    ts["queuedAt"] = int(time.time()) - 3600
        store.refresh_statuses(bid)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "missing")
        self.assertIn("不一致", st["error"])

    def test_task_succeeded_and_cloud_ok_marks_uploaded(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [],
                                 "done": [{"id": "t1", "state": 2}]}
        ctx._client.stats["/backup/a.txt"] = {
            "name": "a.txt", "is_dir": False, "size": 100}
        store.refresh_statuses(bid)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "uploaded")
        self.assertEqual(st["taskId"], "")

    def test_task_succeeded_but_cloud_missing_keeps_queued(self):
        # 任务报成功、云端却看不到文件：跨存储复制有最终一致性窗口，
        # 立刻判失败会误报 → 先保持"排队中"，等超时再定论。
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [],
                                 "done": [{"id": "t1", "state": 2}]}
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "queued")

    def test_task_succeeded_but_size_mismatch_keeps_queued(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [],
                                 "done": [{"id": "t1", "state": 2}]}
        ctx._client.stats["/backup/a.txt"] = {
            "name": "a.txt", "is_dir": False, "size": 7}
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "queued")

    def test_stale_queued_resolved_after_timeout(self):
        # 长时间（默认 30 分钟）拿不到任何结果 → 不能一直挂着，给出结论
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        with store._lock:
            b = store._find(bid)
            for f in b["files"]:
                for ts in (f.get("targets") or {}).values():
                    ts["queuedAt"] = int(time.time()) - 3600
        ctx._client.task_list = {"undone": [],
                                 "done": [{"id": "t1", "state": 2}]}
        store.refresh_statuses(bid)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "missing")
        self.assertIn("重新上传", st["error"])

    def test_task_failed_marks_failed(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [],
                                 "done": [{"id": "t1", "state": 7,
                                           "error": "disk full"}]}
        store.refresh_statuses(bid)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "failed")
        self.assertIn("disk full", st["error"])

    def test_task_running_marks_uploading_with_progress(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [{"id": "t1", "state": 1, "progress": 42}],
                                 "done": []}
        store.refresh_statuses(bid)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "uploading")
        self.assertEqual(st["progress"], 42)


class ScanRobustnessTest(unittest.TestCase):
    def test_source_scan_failure_marks_error(self):
        # 源目录读不到时不能伪装成"扫描完成、无待处理"
        tree = cloud_tree("/source", {"a.txt": 100})
        ctx = make_ctx(tree=tree, scan_errors={"/source": "存储不可用"})
        store = make_store(ctx)
        b = store.create_binding({
            "localPath": "/source", "cloudTargets": ["/backup"]})
        store.start_scan(b["id"], background=False)
        detail = store.get_binding(b["id"])
        self.assertEqual(detail["scanStatus"], "error")
        self.assertIn("存储不可用", detail["scanError"])
        self.assertEqual(detail["files"], [])

    def test_total_local_files_is_source_total(self):
        # 源 3 个文件、其中 2 个缺失 → totalLocalFiles=3，missingCount=2
        ctx, store, bid = ready_ctx(
            files={"a.txt": 100, "b.txt": 200, "done.txt": 300},
            target_cloud={"done.txt": 300})
        detail = store.get_binding(bid)
        summary = next(s for s in store.list_bindings() if s["id"] == bid)
        self.assertEqual(detail["totalLocalFiles"], 3)
        self.assertEqual(summary["totalLocalFiles"], 3)
        self.assertEqual(summary["missingCount"], 2)

    def test_update_name_keeps_scan_result(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        before = len(store.get_binding(bid)["files"])
        self.assertEqual(before, 1)
        store.update_binding(bid, {"name": "改个名字"})
        self.assertEqual(len(store.get_binding(bid)["files"]), 1)
        store.update_binding(bid, {"autoUpload": True})
        self.assertEqual(len(store.get_binding(bid)["files"]), 1)

    def test_update_targets_clears_scan_result(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        store.update_binding(bid, {"cloudTargets": ["/backup2"]})
        self.assertEqual(store.get_binding(bid)["files"], [])


class SnapshotTest(unittest.TestCase):
    """快照模式：源未变 + 上次已同步 → 完全跳过网络核对。"""

    def test_unchanged_source_reuses_previous_verdict(self):
        # 第一次：源与目标一致 → 无待处理，快照记录结论
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                    target_cloud={"a.txt": 100})
        store.update_binding(bid, {"sourceSnapshot": True})
        store.start_scan(bid, background=False)
        self.assertEqual(store.get_binding(bid)["files"], [])
        self.assertTrue(store.get_binding(bid)["snapshot"])

        # 第二次：源没变，但目标端内容被改、且变得不可读。
        # 快照生效就该沿用上次结论（无待处理）；若快照失效去重扫目标，
        # 会因 scan_errors 报出 failed 条目。
        ctx._client.scan_errors["/backup"] = "不该被扫到"
        ctx._client.tree["/backup"] = [{
            "name": "a.txt", "is_dir": False, "size": 999, "modified": ""}]
        store.start_scan(bid, background=False)
        detail = store.get_binding(bid)
        self.assertEqual(detail["scanStatus"], "done")
        self.assertEqual(detail["files"], [])

    def test_changed_source_triggers_recheck(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                    target_cloud={"a.txt": 100})
        store.update_binding(bid, {"sourceSnapshot": True})
        store.start_scan(bid, background=False)
        self.assertEqual(store.get_binding(bid)["files"], [])
        # 源文件变大 → 必须重新核对，发现目标端还是旧的大小
        ctx._client.tree["/source"] = [{
            "name": "a.txt", "is_dir": False, "size": 555, "modified": ""}]
        store.start_scan(bid, background=False)
        st = target_state(store, bid, "a.txt")
        self.assertEqual(st["status"], "mismatch")
        self.assertEqual(st["cloudSize"], 100)

    def test_snapshot_versions_mismatch_forces_full_rescan(self):
        # 旧版本快照格式 → 直接丢弃重扫，不能拿它得出"没变"的假结论
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                    target_cloud={"a.txt": 100})
        store.update_binding(bid, {"sourceSnapshot": True})
        store.start_scan(bid, background=False)
        with store._lock:
            b = store._find(bid)
            b["snapshotVersion"] = 1
            b["snapshot"] = {"a.txt": {"size": 100, "modified": ""},
                             "ghost.txt": {"size": 1, "modified": ""}}
        ctx._client.tree["/backup"] = []
        store.start_scan(bid, background=False)
        # ghost.txt 不该出现在结果里（快照被丢弃，以真实源目录为准）
        detail = store.get_binding(bid)
        rels = [f["relPath"] for f in detail["files"]]
        self.assertEqual(rels, ["a.txt"])


# --------------------------------------------------------------------------
# 扫描性能回归：按父目录批量查询，而不是每个文件一次 stat
# --------------------------------------------------------------------------

class BatchLookupTest(unittest.TestCase):
    """绑定扫描的快照模式：请求数必须随「目录数」增长，而不是「文件数」。"""

    def _snapshot_binding(self, files, target_files):
        ctx, store, bid = ready_ctx(files=files, target_cloud=target_files)
        store.update_binding(bid, {"sourceSnapshot": True})
        # 第一次扫描：快照还是空的 → 走全量模式，同时把各目标结论写进快照
        store.start_scan(bid, background=False)
        # 清掉计数，只统计「第二次扫描」的请求
        ctx._client.list_calls = []
        ctx._client.stat_calls = []
        return ctx, store, bid

    def test_many_files_in_one_dir_use_one_list_request(self):
        files = {}
        files["a.txt"] = 100
        for i in range(40):
            files["d1/f%d.txt" % i] = 10 + i
        # 目标端大小全都不一样 → 每个文件都是 mismatch（未定论）→ 都会被核对
        target = {rel: size + 1 for rel, size in files.items()}
        ctx, store, bid = self._snapshot_binding(files, target)

        store.start_scan(bid, background=False)

        detail = store.get_binding(bid)
        self.assertEqual(len(detail["files"]), len(files),
                         "全部文件都该被判定为需要处理")
        # 关键：41 个文件只应有 2 次目标端列表请求（根目录 + d1），
        # 且**一次 stat 都不该发**（老实现是 41 次 stat）
        target_lists = [p for p in ctx._client.list_calls if p.startswith("/backup")]
        self.assertEqual(sorted(target_lists), ["/backup", "/backup/d1"])
        self.assertEqual(ctx._client.stat_calls, [])

    def test_parent_listing_failure_keeps_previous_verdict(self):
        """目标端「列不出来」≠「云端没有这个文件」：有上次结论就沿用，不得改判成缺失。"""
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                   target_cloud={"a.txt": 50})
        store.update_binding(bid, {"sourceSnapshot": True})
        store.start_scan(bid, background=False)
        first = target_state(store, bid, "a.txt")
        self.assertEqual(first["status"], "mismatch")
        self.assertEqual(first["cloudSize"], 50)

        # 第二次：源没变、上次是 mismatch（未定论）→ 会去核对目标端，
        # 但目标目录这次列不出来
        ctx._client.scan_errors["/backup"] = "网络抽风"
        store.start_scan(bid, background=False)

        kept = target_state(store, bid, "a.txt")
        self.assertEqual(kept["status"], "mismatch",
                         "查询失败不得把「云端是旧版本」改判成「云端缺失」")
        self.assertEqual(kept["cloudSize"], 50, "上次拿到的大小信息要保留")
        self.assertTrue(kept["error"], "应留下「查询失败」的说明，便于排查")

    def test_no_previous_verdict_still_marks_pending_with_reason(self):
        """连上次结论都没有时（新文件）：按待处理处理，但要带上失败原因，不能悄悄放过。"""
        files = {"a.txt": 100, "b.txt": 200}
        ctx, store, bid = ready_ctx(files=files, target_cloud=files)
        store.update_binding(bid, {"sourceSnapshot": True})
        store.start_scan(bid, background=False)      # 全量模式，两边一致 → uploaded
        with store._lock:
            self.assertTrue(store._find(bid)["snapshot"], "快照应为非空，否则会退回全量模式")

        # 源端新增一个目标端没有的文件；同时目标目录变得列不出来
        ctx._client.tree["/source"].append({
            "name": "c.txt", "is_dir": False, "size": 300, "modified": ""})
        ctx._client.scan_errors["/backup"] = "网络抽风"
        store.start_scan(bid, background=False)

        st = target_state(store, bid, "c.txt")
        self.assertEqual(st["status"], "missing")
        self.assertIn("查询失败", st["error"])
        # 老文件已同步且源未变 → 仍在快照里复用结论，不受目标端故障影响
        detail = store.get_binding(bid)
        self.assertEqual([f["relPath"] for f in detail["files"]], ["c.txt"])


class TaskFreshnessTest(unittest.TestCase):
    """任务列表必须每次现查 —— 这条路径上「新鲜」比「省一次请求」重要。

    背景：v2.8.5 优化时曾给 `_fetch_tasks` 加过 1.5 秒 TTL 缓存，想省掉列表页
    轮询的重复请求。但实测**一次都省不下来**：所有调用点都是「先确认有在途任务，
    才来拉任务列表」（没有在途任务时压根不调用），而前端轮询间隔本来就 ≥ 2 秒，
    大于 TTL；唯一的效果是把「任务已被服务端清理 → 该判缺失」这类**状态转换**
    推迟到缓存过期之后。端到端冒烟当场抓到（见 tests/smoke_e2e.py 第 10 步），
    所以缓存被移除，并在此钉住「不缓存」这个不变量。
    """

    def test_repeated_fetch_is_not_cached(self):
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store._fetch_tasks()
        store._fetch_tasks()
        store._fetch_tasks()
        self.assertEqual(ctx._client.task_fetches, 3,
                         "有在途任务时每次判定都必须拿最新任务列表，不得复用旧结果")

    def test_fetch_failure_is_not_cached(self):
        """查不到也得每次重试：服务端刚重启时正是靠下一次查询恢复的。"""
        ctx, store, bid = ready_ctx(tasks_supported=False)
        self.assertIsNone(store._fetch_tasks())
        self.assertIsNone(store._fetch_tasks())
        self.assertEqual(ctx._client.task_fetches, 2)

    def test_task_disappearing_is_seen_on_next_call(self):
        """服务端把任务清掉后，**紧接着的下一次**状态同步就必须反映出来（不得延后）。

        这是 v2.8.5 加任务列表缓存那次真正踩到的坑：两次同步之间不放任何等待，
        一旦中间隔了缓存，第二次仍会读到「任务还在」，状态就卡在上传中。
        所以这里断言的是「不再停留在过期的上传中」——id 查不到之后要立刻转入
        云端核对/宽限判断。

        云端此刻还没有文件，但这次复制才刚提交不久，因此不能立刻判缺失
        （宽限期行为见 VerdictTest.test_task_id_not_found_keeps_in_flight_within_grace；
        超时后的定论见 test_task_gone_marks_missing_after_timeout）。
        """
        ctx, store, bid = ready_ctx(copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [{"id": "t1", "name": "a.txt", "state": 1, "progress": 10}],
                                 "done": []}
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "uploading")

        # OpenList 清空任务列表（重启/清理）
        ctx._client.task_list = {"undone": [], "done": []}
        before = len(ctx._client.list_calls) + len(ctx._client.stat_calls)
        store.refresh_statuses(bid)
        after = len(ctx._client.list_calls) + len(ctx._client.stat_calls)
        # 判据是「真的去云端核对了」：id 查不到之后必须转入云端证据。
        # 若中途隔了任务列表缓存，这里会继续读到「任务还在」，
        # 一次云端查询都不会发生 —— 那才是要防的延后。
        self.assertGreater(after, before,
                           "任务消失后必须立刻转入云端核对，而不是沿用旧任务列表")
        # 云端此刻还没有文件，但这次复制才刚提交不久 → 保持进行中，
        # 不急于判缺失（宽限期与超时后的定论见 VerdictTest 里的两个用例）。
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "uploading")


class BindingTaskListTest(unittest.TestCase):
    """传输列表里的绑定任务：既要看得见，也不能把接口拖垮。

    这一组钉住的是一个真实反馈：绑定备份里对一个文件夹点「一键上传」，
    云端确实在复制，但传输列表里看不到、界面像"没反应"。
    """

    def test_binding_task_survives_missing_task_id(self):
        """任务 id 在任务列表里查不到时，刚提交的任务不能立刻从列表消失。

        OpenList 完成任务后会清理任务记录，跨存储复制返回的 tasks id 与
        `/api/task/copy/*` 里的 id 也未必同源 —— 这些都会表现成"id 查不到"。
        """
        ctx, store, bid = ready_ctx(files={"sub/a.txt": 100},
                                    copy_tasks=["t1"], tasks_supported=True)
        ctx._client.task_list = {"undone": [], "done": []}      # 任务查不到
        store.upload_files(bid, rel_paths=["sub/a.txt"])
        tasks = store.list_binding_tasks()
        self.assertEqual(len(tasks), 1, "刚提交的任务必须仍然出现在传输列表里")
        self.assertTrue(tasks[0]["isBinding"])
        self.assertEqual(tasks[0]["bindingId"], bid)

    def test_binding_task_row_identifies_binding_and_folder(self):
        """行里要能看出「是哪个绑定、哪个文件夹」，否则用户以为传丢了。"""
        ctx, store, bid = ready_ctx(files={"sub/deep/a.txt": 100},
                                    copy_tasks=None)            # 同存储复制不建任务
        store.upload_files(bid, rel_paths=["sub/deep/a.txt"])
        tasks = store.list_binding_tasks()
        self.assertEqual(len(tasks), 1)
        row = tasks[0]
        self.assertEqual(row["name"], "a.txt")
        self.assertIn("sub/deep", row["remotePath"], "目标路径要带上文件夹层级")
        self.assertIn("source", row["remotePath"])

    def test_cloud_verification_is_batched(self):
        """500 个进行中的文件只按父目录列一次，不能每个文件一次请求。

        传输列表每 2 秒轮询一次；逐文件核对的话，刚点完「一键上传」的那几秒
        每一轮都会打出几百次请求，接口被拖慢，界面表现就是"点了没反应"。
        """
        n = 500
        files = {"sub/f%04d.txt" % i: 100 for i in range(n)}
        ctx, store, bid = ready_ctx(files=files, copy_tasks=None)
        store.upload_files(bid, rel_paths=list(files))

        ctx._client.list_calls.clear()
        ctx._client.stat_calls.clear()
        tasks = store.list_binding_tasks()
        cloud = len(ctx._client.list_calls) + len(ctx._client.stat_calls)
        self.assertEqual(len(tasks), n)
        # 500 个文件都在同一个目录 → 一次列表请求就够
        self.assertLessEqual(cloud, 2,
                             "必须按父目录批量列，实际发了 %d 次请求" % cloud)

    def test_folder_upload_matches_by_set(self):
        """一次提交大批路径时匹配要正确（且不能是列表线性查找）。"""
        n = 500
        files = {"sub/f%04d.txt" % i: 100 for i in range(n)}
        ctx, store, bid = ready_ctx(files=files)
        submitted = store.upload_files(bid, rel_paths=list(files))
        self.assertEqual(submitted, n)
        detail = store.get_binding(bid)
        self.assertTrue(all(
            list(f["targets"].values())[0]["status"] == "queued"
            for f in detail["files"]))

    def test_upload_list_no_longer_merges_binding_tasks(self):
        """传输列表不再合并绑定文件级任务：直接引用 OpenList 任务列表（/api/openlist/tasks）。

        即使 list_binding_tasks 失败也不影响 /api/upload/list —— 两者已经解耦。
        """
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        bstore = ctx.bindings()

        def boom():
            raise RuntimeError("绑定数据读取失败")

        bstore.list_binding_tasks = boom
        st, d = jcall(ctx, "GET", "/api/upload/list")
        self.assertEqual(st, 200, "list_binding_tasks 失败不应影响 /api/upload/list")
        self.assertEqual(d.get("tasks"), [], "本地队列为空且不合并绑定任务时应为空")
        self.assertNotIn("bindingError", d, "不再有 bindingError 字段")

    def test_finished_binding_task_stays_visible_as_done(self):
        """复制完成后要以「已完成」留在传输列表里，不能凭空消失。

        真实反馈：绑定备份里传完一个文件夹，传输列表里那条任务就没了 ——
        用户看到的是「传完那一刻任务不见了」。而本地上传队列的终态任务是
        **保留**在列表里的，两者表现必须一致。
        """
        ctx, store, bid = ready_ctx(files={"sub/a.txt": 100},
                                    copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["sub/a.txt"])
        ctx._client.task_list = {"undone": [], "done": [{"id": "t1", "state": 2}]}
        ctx._client.stats["/backup/sub/a.txt"] = {
            "name": "a.txt", "is_dir": False, "size": 100}
        store.refresh_statuses(bid)
        self.assertEqual(target_state(store, bid, "sub/a.txt")["status"], "uploaded")

        tasks = store.list_binding_tasks()
        self.assertEqual(len(tasks), 1, "完成后仍要留在传输列表里")
        self.assertEqual(tasks[0]["status"], "done")
        self.assertTrue(tasks[0]["isBinding"])
        self.assertEqual(tasks[0]["uploaded"], 100, "已完成要显示满进度")

    def test_finished_task_drops_out_after_retention(self):
        """超过保留窗口就不再显示，免得列表越积越长（尤其开了自动上传时）。"""
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                    copy_tasks=["t1"], tasks_supported=True)
        store.upload_files(bid, rel_paths=["a.txt"])
        ctx._client.task_list = {"undone": [], "done": [{"id": "t1", "state": 2}]}
        ctx._client.stats["/backup/a.txt"] = {
            "name": "a.txt", "is_dir": False, "size": 100}
        store.refresh_statuses(bid)
        self.assertEqual(len(store.list_binding_tasks()), 1)

        stale = (bindings_mod._now()
                 - bindings_mod.BINDING_DONE_VISIBLE_SECONDS - 60)
        with store._lock:
            for b in store._bindings:
                for f in b.get("files", []):
                    for ts in (f.get("targets") or {}).values():
                        ts["doneAt"] = stale
        self.assertEqual(store.list_binding_tasks(), [],
                         "超出保留窗口的已完成任务应当淡出，不能无限堆积")

    def test_finished_sorted_before_inflight(self):
        """前端会整体 reverse（最新在最上）：后端要已完成在前、进行中在后。

        这样 reverse 之后进行中排在最显眼的位置，已完成落在下方。
        """
        ctx, store, bid = ready_ctx(files={"a.txt": 100, "b.txt": 200},
                                    copy_tasks=["t1", "t2"], tasks_supported=True)
        store.upload_files(bid, upload_all=True)
        ctx._client.task_list = {"undone": [{"id": "t2", "state": 1}],
                                 "done": [{"id": "t1", "state": 2}]}
        ctx._client.stats["/backup/a.txt"] = {
            "name": "a.txt", "is_dir": False, "size": 100}
        store.refresh_statuses(bid)
        got = [t["status"] for t in store.list_binding_tasks()]
        self.assertEqual(got, ["done", "uploading"],
                         "已完成要排在前面，进行中排后面")

    def test_synced_file_never_uploaded_not_listed(self):
        """扫描时云端本来就有、从未经过本绑定上传的文件，不该混进传输列表。

        靠「有没有上传痕迹（doneAt / queuedAt）」区分：没有痕迹的 uploaded
        只是"本来就同步"，不是"刚刚传完"。
        """
        ctx, store, bid = ready_ctx(files={"a.txt": 100},
                                    target_cloud={"a.txt": 100})
        self.assertEqual(store.get_binding(bid)["files"], [],
                         "已同步的文件本来就该移出待处理列表")
        self.assertEqual(store.list_binding_tasks(), [],
                         "从未上传过的文件不得出现在传输列表")


# --------------------------------------------------------------------------
# 提交上传：进度可观测 + 防重复提交（v2.8.8）
# --------------------------------------------------------------------------

class SubmitProgressTest(unittest.TestCase):
    """「一键上传」不是一个瞬间动作。

    每个子目录一组 `ensure_dir` + `copy`：选一个含 30 个子目录的文件夹就是
    60 次网络往返，十几秒起步。这段时间里界面上必须有真实进度可看、重复点击
    必须被挡住 —— 否则用户看到的就是「点了没反应」，然后反复点击，
    而每一次点击都会往 OpenList 多发一份复制请求。
    """

    # 3 个目录组：根（a.txt, b.txt）/ sub（c.txt, d.txt）/ sub2（e.txt）
    FILES = {"a.txt": 100, "b.txt": 200,
             "sub/c.txt": 300, "sub/d.txt": 400,
             "sub2/e.txt": 500}
    GROUPS = 3
    TOTAL_FILES = 5

    def setUp(self):
        self.ctx, self.store, self.bid = ready_ctx(files=dict(self.FILES))

    # -- 辅助 --

    def _submit(self):
        return self.store.upload_files(self.bid, upload_all=True)

    def _progress(self):
        """只查「提交阶段」的进度（前端进度条走的就是这条路径）。"""
        return self.store.get_progress(self.bid, submit_only=True)["submit"]

    def _set_concurrency(self, workers):
        cfg = config.load_config()
        cfg["scanConcurrency"] = workers
        config.save_config(cfg)

    def _blocked_submit(self):
        """在另一个线程发起提交，并让它卡在第一次 copy 上。

        返回 (thread, entered, release)。用 Event 而不是 sleep 来同步：
        并发提交下按时间猜状态必然不稳定。
        """
        entered = threading.Event()
        gate = threading.Event()

        def hook(src, dst, names):
            entered.set()
            gate.wait(5)

        self.ctx._client.on_copy = hook
        thread = threading.Thread(target=self._submit)
        thread.start()
        return thread, entered, gate.set

    # -- 进度可观测 --

    def test_progress_is_visible_while_submitting(self):
        """提交进行中必须能拿到「进行中」的真实进度，而不是只有一个转圈。"""
        samples = []
        self.ctx._client.on_copy = lambda src, dst, names: samples.append(
            self._progress())

        count = self._submit()

        self.assertEqual(count, self.TOTAL_FILES)
        self.assertEqual(len(samples), self.GROUPS,
                         "每个目录组提交前都该能采样到一次进度")
        for s in samples:
            self.assertTrue(s["active"], "提交还没结束，进度必须是进行中")
            self.assertEqual(s["phase"], bindings_mod.SUBMIT_SENDING)
            self.assertEqual(s["total"], self.GROUPS,
                             "分母是目录组数 —— 用户等的正是这几十次往返")
            self.assertEqual(s["files"], self.TOTAL_FILES)
            self.assertLess(s["done"], s["total"],
                            "采样发生在该组 copy 之前，它此刻还不算完成")

        # 收尾：进度必须走到 100%，不能停在半路
        done = self._progress()
        self.assertFalse(done["active"])
        self.assertEqual(done["phase"], bindings_mod.SUBMIT_DONE)
        self.assertEqual(done["submitted"], self.TOTAL_FILES)
        self.assertEqual(done["done"], self.GROUPS)
        self.assertEqual(done["percent"], 100)

    def test_submit_only_progress_touches_no_cloud(self):
        """轻量进度查询绝不能去打云端：前端在提交期间要每秒刷好几次。"""
        before = (len(self.ctx._client.list_calls),
                  len(self.ctx._client.stat_calls),
                  self.ctx._client.task_fetches)
        prog = self.store.get_progress(self.bid, submit_only=True)
        after = (len(self.ctx._client.list_calls),
                 len(self.ctx._client.stat_calls),
                 self.ctx._client.task_fetches)
        self.assertEqual(before, after, "提交进度查询不该产生任何云端请求")
        self.assertIn("submit", prog)

    def test_progress_view_is_always_complete(self):
        """没有任何提交记录时也要返回字段齐全的视图，前端不必到处判空。"""
        ctx, store, bid = ready_ctx(files={"a.txt": 100})
        prog = store.get_progress(bid, submit_only=True)["submit"]
        self.assertFalse(prog["active"])
        self.assertEqual(prog["phase"], "")
        self.assertEqual(prog["total"], 0)
        self.assertEqual(prog["percent"], 100)
        for key in ("done", "files", "targets", "submitted", "failed",
                    "message", "startedAt", "finishedAt", "elapsed"):
            self.assertIn(key, prog)

    # -- 防重复提交 --

    def test_duplicate_submit_is_rejected_while_running(self):
        """提交进行中再点一次必须被明确拒绝 —— 而不是又提交一份。"""
        thread, entered, release = self._blocked_submit()
        try:
            self.assertTrue(entered.wait(5), "提交线程没能进到 copy")
            with self.assertRaises(ValueError) as cm:
                self.store.upload_files(self.bid, rel_paths=["a.txt"])
            self.assertIn("正在提交", str(cm.exception))
            self.assertEqual(len(self.ctx._client.copies), 0,
                             "被拒的那一次不该发出任何复制请求")
        finally:
            release()
            thread.join(5)
        self.assertFalse(thread.is_alive(), "第一次提交应当正常跑完")
        self.assertFalse(self.store.is_submitting(self.bid),
                         "提交结束后闸门必须放开，否则这个绑定再也传不了")

    def test_submitting_flag_is_exposed_in_binding_list(self):
        """刷新页面后按钮要能恢复禁用态，靠的就是列表里的 submitting。"""
        self.assertFalse(self._row()["submitting"])

        thread, entered, release = self._blocked_submit()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self._row()["submitting"],
                            "提交中必须在绑定列表里体现出来")
        finally:
            release()
            thread.join(5)
        self.assertFalse(self._row()["submitting"])

    def _row(self):
        return next(r for r in self.store.list_bindings() if r["id"] == self.bid)

    # -- 并发与串行等价 --

    def test_concurrent_submit_matches_sequential(self):
        """并发提交只是更快；交出去的东西必须与串行逐项一致。"""

        def run(workers):
            ctx, store, bid = ready_ctx(files=dict(self.FILES))
            # 配置要在 ctx 建好之后再写：make_ctx 会切到一个全新的隔离数据目录。
            cfg = config.load_config()
            cfg["scanConcurrency"] = workers
            config.save_config(cfg)
            count = store.upload_files(bid, upload_all=True)
            copies = sorted((src, dst, tuple(names))
                            for src, dst, names, _kw in ctx._client.copies)
            return count, copies

        seq = run(1)
        par = run(8)

        self.assertEqual(seq[0], self.TOTAL_FILES)
        self.assertEqual(par[0], self.TOTAL_FILES)
        self.assertEqual(seq, par,
                         "并发与串行提交的文件集合必须完全一致")
        self.assertEqual(len(par[1]), self.GROUPS,
                         "同目录的多个文件应当合并成一次 copy")

    # -- 失败路径 --

    def test_failed_group_still_advances_progress(self):
        """失败的那组也要让进度前进：否则进度条永远卡在半路。"""
        lock = threading.Lock()
        seen = {"n": 0}

        def hook(src, dst, names):
            with lock:
                seen["n"] += 1
                first = seen["n"] == 1
            if first:
                raise openlist.OpenlistError("disk full")

        self.ctx._client.on_copy = hook
        count = self._submit()

        self.assertGreater(count, 0, "只失败一组，其余必须正常提交")
        self.assertLess(count, self.TOTAL_FILES)
        prog = self._progress()
        self.assertFalse(prog["active"])
        self.assertEqual(prog["phase"], bindings_mod.SUBMIT_DONE,
                         "部分失败不算提交失败 —— 多数文件已经交出去了")
        self.assertEqual(prog["done"], self.GROUPS, "失败的那组也要算进进度")
        self.assertEqual(prog["percent"], 100)
        self.assertEqual(prog["failed"], 1)
        self.assertTrue(prog["message"], "部分失败必须给出人话说明")

    def test_all_failed_submit_releases_the_gate(self):
        """一次失败不能让绑定永久卡在「正在提交」。"""
        self.ctx._client.copy_error = openlist.OpenlistError("boom")
        with self.assertRaises(ValueError):
            self._submit()
        self.assertFalse(self.store.is_submitting(self.bid),
                         "异常路径也必须收尾，否则这个绑定再也传不了")
        prog = self._progress()
        self.assertFalse(prog["active"])
        self.assertEqual(prog["phase"], bindings_mod.SUBMIT_FAILED)
        self.assertTrue(prog["message"], "失败原因要让用户看得见")


class SubmitRouteTest(unittest.TestCase):
    """接口层：进度查询要支持轻量模式，提交结果要当场带回结论。"""

    def test_progress_route_supports_submit_only(self):
        ctx = make_ctx()
        store = ctx.bindings()
        b = store.create_binding({"localPath": "/source",
                                  "cloudTargets": ["/backup"]})
        status, data = jcall(
            ctx, "GET", "/api/bindings/progress?id=%s&submit=1" % b["id"])
        self.assertEqual(status, 200)
        submit = data["progress"]["submit"]
        self.assertFalse(submit["active"])
        self.assertIn("phase", submit)

    def test_progress_route_resolves_binding_from_querystring(self):
        """按 id 必须能查到这个绑定。

        查询参数经 `parse_qs` 解析后是 **list**，而这里原来是全项目唯一一处
        忘了取首值的 query 读取 —— 于是 `_find` 永远匹配不上，接口一直返回
        `progress: null`，前端那个 2 秒一次的进度轮询完全是白转，
        用户从来没见过复制进度。
        """
        tree = cloud_tree("/source", {"a.txt": 100})
        ctx = make_ctx(tree=tree)
        store = ctx.bindings()
        b = store.create_binding({"localPath": "/source",
                                  "cloudTargets": ["/backup"]})
        status, data = jcall(ctx, "GET",
                             "/api/bindings/progress?id=" + b["id"])
        self.assertEqual(status, 200)
        self.assertIsNotNone(data["progress"], "按 id 必须能查到绑定")
        self.assertIn("hasActive", data["progress"])

    def test_upload_route_returns_submit_summary(self):
        """提交接口顺手把结论带回来，省得前端再问一次。"""
        tree = cloud_tree("/source", {"sub/a.txt": 100})
        ctx = make_ctx(tree=tree)
        store = ctx.bindings()
        b = store.create_binding({"localPath": "/source",
                                  "cloudTargets": ["/backup"]})
        store.start_scan(b["id"], background=False)

        status, data = jcall(ctx, "POST", "/api/bindings/upload",
                             {"id": b["id"], "all": True})

        self.assertEqual(status, 200)
        self.assertEqual(data["queued"], 1)
        submit = data["submit"]
        self.assertFalse(submit["active"])
        self.assertEqual(submit["phase"], bindings_mod.SUBMIT_DONE)
        self.assertEqual(submit["submitted"], 1)
        self.assertEqual(submit["percent"], 100)


# --------------------------------------------------------------------------
# 目录创建的请求数（v2.8.9 回归）
# --------------------------------------------------------------------------

def bulk_files(n_dirs=5, n_subs=6, n_files=4):
    """造 n_dirs × n_subs × n_files 个文件的相对路径表。"""
    out = {}
    for i in range(n_dirs):
        for j in range(n_subs):
            for k in range(n_files):
                out["d%d/s%d/f%d.txt" % (i, j, k)] = 100
    return out


def cloud_dirs_tree(n_dirs=5, n_subs=6):
    """造出 `/backup` 及其全部子目录 —— 模拟「目录早就存在」的日常状态。

    注意必须同时提供 `/` 这一级，否则假客户端从 `tree["/"]` 找不到
    `backup` 这个节点，会误判成「云端没有这个目录」。
    """
    tree = {"/": [{"name": "backup", "is_dir": True, "size": 0}],
            "/backup": []}
    for i in range(n_dirs):
        tree["/backup"].append({"name": "d%d" % i, "is_dir": True, "size": 0})
        tree["/backup/d%d" % i] = []
        for j in range(n_subs):
            tree["/backup/d%d" % i].append(
                {"name": "s%d" % j, "is_dir": True, "size": 0})
            tree["/backup/d%d/s%d" % (i, j)] = []
    return tree


class DirEnsureRequestCountTest(unittest.TestCase):
    """目录创建不该把同一批父目录反复查一遍。

    背景：`ensure_dir` 以前对路径的**每一级**都发一次 `fs/get`，而且并发
    worker 之间完全不共享结果。实测提交 30 个子目录 / 120 个文件时发了
    156~177 次请求，其中 54 次 stat + 18 次 mkdir 是纯重复 —— 而 OpenList
    网页上手动复制同一个文件夹只需 1 次请求。这就是「软件比 OpenList 还慢」
    的主要来源。这组用例把**请求数**钉住，防止优化被后续修改悄悄回退。
    """

    N_DIRS, N_SUBS, N_FILES = 5, 6, 4

    def _upload(self, cloud_ready=True):
        files = bulk_files(self.N_DIRS, self.N_SUBS, self.N_FILES)
        extra = cloud_dirs_tree(self.N_DIRS, self.N_SUBS) if cloud_ready else None
        ctx, store, bid = ready_ctx(files=files, extra=extra)
        client = ctx._client
        # 扫描阶段的请求不算数 —— 这里只看「提交」这一段。
        client.list_calls[:] = []
        client.stat_calls[:] = []
        client.mkdir_calls[:] = []
        client.copies[:] = []
        n = store.upload_files(bid, upload_all=True)
        return client, n

    def test_existing_dirs_cost_one_lookup_each(self):
        """目录已存在（日常）：每个唯一目录只查一次，与路径深度无关。"""
        client, n = self._upload(cloud_ready=True)
        self.assertEqual(n, self.N_DIRS * self.N_SUBS * self.N_FILES,
                         "所有文件都应提交（并发提交不得改变结果）")
        self.assertEqual(len(client.stat_calls), len(set(client.stat_calls)),
                         "同一个目录路径不该被查第二遍")
        # 唯一目录 = 1(/backup) + 5(d*) + 30(s*) = 36；旧实现是 90
        self.assertLessEqual(
            len(client.stat_calls), 40,
            "请求数应与「目录数」同阶，而不是「目录数 × 路径深度」")

    def test_total_requests_stay_near_copy_count(self):
        """总请求数应接近「目录组数」，不该被目录查询淹没。"""
        client, _n = self._upload(cloud_ready=True)
        total = len(client.copies) + len(client.stat_calls) + len(client.mkdir_calls)
        self.assertEqual(len(client.copies), self.N_DIRS * self.N_SUBS,
                         "每个子目录一组 copy")
        self.assertLessEqual(total, 70, "优化前这里是 120 次")

    def test_concurrent_mkdir_not_duplicated(self):
        """8 路并发下，同一个目录只该被 mkdir 一次。"""
        client, _n = self._upload(cloud_ready=False)
        self.assertEqual(len(client.mkdir_calls), len(set(client.mkdir_calls)),
                         "并发 worker 各建一遍同一个目录是纯浪费")
        self.assertLessEqual(len(client.mkdir_calls), 40)

    def test_parent_lookup_shared_across_siblings(self):
        """同父目录下的兄弟目录，父级只查一次。"""
        client = BindingFakeClient(tree=cloud_dirs_tree(2, 3))
        cache = openlist.DirCache()
        client.ensure_dir("/backup/d0/s0", cache)
        client.ensure_dir("/backup/d0/s1", cache)
        self.assertEqual(client.stat_calls.count("/backup"), 1)
        self.assertEqual(client.stat_calls.count("/backup/d0"), 1)

    def test_cache_short_circuits_second_call(self):
        client = BindingFakeClient(tree=cloud_dirs_tree(1, 1))
        cache = openlist.DirCache()
        client.ensure_dir("/backup/d0/s0", cache)
        before = len(client.stat_calls)
        client.ensure_dir("/backup/d0/s0", cache)
        self.assertEqual(len(client.stat_calls), before,
                         "第二次必须直接命中缓存，一个请求都不发")
        self.assertGreater(cache.hits, 0, "缓存要真的被用到，而不是碰巧通过")

    def test_path_occupied_by_file_still_raises(self):
        """优化不能把「路径被同名文件占住」这个错误吞掉。"""
        client = BindingFakeClient(tree={"/": [{"name": "backup", "size": 10}]})
        with self.assertRaises(openlist.OpenlistError):
            client.ensure_dir("/backup/sub")

    def test_works_without_shared_cache(self):
        """不传 cache 的调用点（上传队列、建目录接口）也必须照常工作。"""
        client = BindingFakeClient(tree={})
        client.ensure_dir("/a/b/c")
        self.assertIn("/a/b/c", client.mkdir_calls)
        self.assertEqual(client.mkdir_calls, ["/a", "/a/b", "/a/b/c"],
                         "必须从浅到深逐级建（mkdir 不会自动建父级）")


class SubmitQuietTest(unittest.TestCase):
    """提交进行中，传输列表不得同时去云端核对。

    那些请求不仅白费（云端此刻必然还没有文件），还会和提交本身抢 OpenList：
    传输列表每 2 秒要发 30 次 `fs/list`，正好压在提交最需要带宽的那几秒上。
    """

    def test_no_cloud_lookup_while_submitting(self):
        ctx, store, bid = ready_ctx(files={"a.txt": 100, "b.txt": 200})
        # 先提交 a.txt —— 它会停在 queued（在途），传输列表本来每轮都会去核对它
        store.upload_files(bid, rel_paths=["a.txt"])
        self.assertEqual(target_state(store, bid, "a.txt")["status"], "queued")

        seen = {}

        def during(src_dir, dst_dir, names):
            # 此刻正在提交 b.txt：调一次传输列表，看它会不会去打云端
            client = ctx._client
            client.list_calls[:] = []
            client.task_fetches = 0
            rows = store.list_binding_tasks()
            seen["list"] = len(client.list_calls)
            seen["fetches"] = client.task_fetches
            seen["rows"] = len(rows)

        ctx._client.on_copy = during
        store.upload_files(bid, rel_paths=["b.txt"])

        self.assertEqual(seen.get("list"), 0,
                         "提交进行中不该去列云端目录（会和提交抢 OpenList）")
        self.assertEqual(seen.get("fetches"), 0,
                         "连任务列表都不必拉：这一轮没有需要核对的文件")
        self.assertGreater(seen.get("rows"), 0,
                           "但列表照常显示，用户得看得到它正在传")


if __name__ == "__main__":
    unittest.main(verbosity=2)
