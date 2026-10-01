# -*- coding: utf-8 -*-
"""应用接口层测试：进程内直接驱动 BaseHTTPRequestHandler（不占端口、不依赖网络）。

重点：网关前缀剥离、本地路径白名单（越权必须 400）、连接/浏览/上传/对比
各接口的返回形态，以及超大请求体的 keep-alive 处理。
"""
import json
import os
import tempfile
import unittest

import support

import config
import download_queue
import http_api
import openlist
import upload_queue


class FakeClient(object):
    def __init__(self):
        self.login_args = None
        self.tree = {"/": [{"name": "movies", "is_dir": True, "size": 0, "modified": ""},
                           {"name": "readme.txt", "is_dir": False, "size": 12, "modified": ""}],
                     "/movies": []}
        self.removed = []
        self.removed_fail = []
        self.copied = []
        self.copy_fail = set()
        self.downloads = []
        self.raw_urls = {}

    def login(self, base, username, password, otp=""):
        self.login_args = (base, username, password, otp)
        return {"configured": True, "baseUrl": base, "username": username}

    def logout(self):
        pass

    def is_configured(self):
        return True

    def me(self):
        return {"username": "alice", "base_path": "/", "permission": 0b1000}

    def list_dir(self, path, page=1, per_page=1000):
        return {"content": self.tree.get(path, []), "total": len(self.tree.get(path, [])),
                "write": True, "provider": "local"}

    def list_all(self, path, meta=None):
        data = self.list_dir(path)
        if meta is not None:
            meta["write"] = data["write"]
            meta["provider"] = data["provider"]
        return self.tree.get(path, [])

    def ensure_dir(self, path):
        return None

    def stat(self, path):
        parent, _, name = path.rstrip("/").rpartition("/")
        for node in self.tree.get(parent or "/", []):
            if node["name"] == name:
                out = dict(node)
                out["raw_url"] = self.raw_urls.get(path, "/d" + path)
                return out
        return None

    def remove_many(self, paths):
        self.removed.extend(paths)
        return [p for p in paths if p not in self.removed_fail], \
            [{"path": p, "error": "模拟失败"} for p in paths if p in self.removed_fail]

    def copy(self, src_dir, dst_dir, names, overwrite=False, skip_existing=False,
             merge=False):
        """OpenList 服务端复制（云端↔云端走这条）。"""
        self.copied.append({"src": src_dir, "dst": dst_dir, "names": list(names),
                            "overwrite": overwrite})
        bad = [n for n in names if n in self.copy_fail]
        if bad:
            raise openlist.OpenlistError("file [%s] exists" % bad[0], code=403)
        return {"tasks": []}

    def download_to_file(self, remote, local, on_progress=None,
                         should_cancel=None, expected_size=None):
        self.downloads.append({"remote": remote, "local": local})
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(b"payload")
        return {"size": 7, "path": local}

    def diagnose(self):
        return {"ok": True, "steps": [{"name": "版本", "ok": True, "detail": "v4.2.6"}]}


def make_ctx(client=None):
    root = support.temp_data_dir()
    os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = root
    ctx = http_api.AppContext(config.APP_NAME)
    ctx._store = openlist.CredentialStore(os.path.join(root, "cred.json"))
    ctx._client = client or FakeClient()
    ctx._queue = upload_queue.UploadQueue(
        ctx._client, path=os.path.join(root, "q.json"))   # 不 start，worker 不跑
    # 下载队列同样**不 start**：测试直接驱动它的方法，时序才可控
    ctx._downloads = download_queue.DownloadQueue(
        ctx._client, path=os.path.join(root, "dq.json"))
    ctx._sandbox = root
    return ctx


def call(ctx, method, path, body=None):
    return support.call_handler(http_api.AppHandler, ctx, method, path, body=body)


def jcall(ctx, method, path, body=None):
    status, headers, raw = call(ctx, method, path, body=body)
    return status, support.json_body(raw)


class PrefixTest(unittest.TestCase):
    def test_strip_variants(self):
        self.assertEqual(http_api.strip_prefix("/app/cloud-sync/api/info", "cloud-sync"),
                         "/api/info")
        self.assertEqual(http_api.strip_prefix("/APP/DOUBAO-DRIVE//api/info", "cloud-sync"),
                         "/api/info")
        self.assertEqual(http_api.strip_prefix("/", "cloud-sync"), "/")


class WhitelistTest(unittest.TestCase):
    def test_allowed(self):
        roots = ["/vol1", "/vol2"]
        self.assertTrue(http_api.is_path_allowed("/vol1/1000/x", roots))
        self.assertFalse(http_api.is_path_allowed("/etc/passwd", roots))
        self.assertFalse(http_api.is_path_allowed("", roots))


class InfoTest(unittest.TestCase):
    def test_info(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], "cloud-sync")
        self.assertIn("version", data)
        self.assertIn("hashBackend", data)


class ConnectionTest(unittest.TestCase):
    def test_connection_unconfigured(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "GET", "/api/connection")
        self.assertEqual(status, 200)
        self.assertFalse(data["connection"]["configured"])

    def test_connect_calls_login(self):
        client = FakeClient()
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/connect",
                             {"baseUrl": "http://nas:5244", "username": "alice",
                              "password": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(client.login_args[0], "http://nas:5244")
        self.assertEqual(client.login_args[1], "alice")
        self.assertEqual(client.login_args[2], "secret")

    def test_connect_missing_password(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/connect",
                             {"baseUrl": "http://x", "username": "a"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])


class LocalListTest(unittest.TestCase):
    def test_within_whitelist(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "docs")
        os.makedirs(d)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("hi")
        status, data = jcall(ctx, "GET", "/api/local/list?path=" + d.replace("\\", "/"))
        self.assertEqual(status, 200)
        names = [e["name"] for e in data["entries"]]
        self.assertIn("a.txt", names)

    def test_outside_whitelist_rejected(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "GET", "/api/local/list?path=/etc")
        self.assertEqual(status, 400)
        self.assertIn("允许访问", data["error"])


class CloudListTest(unittest.TestCase):
    def test_cloud_list(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "GET", "/api/cloud/list?path=/")
        self.assertEqual(status, 200)
        names = [e["name"] for e in data["entries"]]
        self.assertIn("movies", names)
        # 目录排前
        self.assertTrue(data["entries"][0]["isDir"])


class EnqueueTest(unittest.TestCase):
    def _make_local(self, ctx):
        f = os.path.join(ctx._sandbox, "up.txt")
        with open(f, "w") as fh:
            fh.write("upload me")
        return f

    def test_enqueue_items_mode(self):
        ctx = make_ctx()
        f = self._make_local(ctx)
        status, data = jcall(ctx, "POST", "/api/upload/enqueue",
                             {"items": [{"localPath": f, "remotePath": "/dst/up.txt"}]})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 1)
        task = ctx._queue.list()[0]
        self.assertEqual(task["remotePath"], "/dst/up.txt")
        self.assertEqual(task["status"], "pending")

    def test_enqueue_browse_mode_expands_dir(self):
        ctx = make_ctx()
        sub = os.path.join(ctx._sandbox, "folder")
        os.makedirs(os.path.join(sub, "inner"))
        for name in ("a.txt", "inner/b.txt"):
            with open(os.path.join(sub, name), "w") as fh:
                fh.write("x")
        status, data = jcall(ctx, "POST", "/api/upload/enqueue",
                             {"localPaths": [sub], "cloudDir": "/cloud"})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 2)
        remotes = sorted(t["remotePath"] for t in ctx._queue.list())
        self.assertEqual(remotes, ["/cloud/folder/a.txt", "/cloud/folder/inner/b.txt"])

    def test_enqueue_outside_whitelist_rejected(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/upload/enqueue",
                             {"items": [{"localPath": "/etc/passwd", "remotePath": "/x"}]})
        self.assertEqual(status, 400)
        self.assertIn("允许访问", data["error"])

    def test_enqueue_reports_real_size_without_extra_stat(self):
        """一键上传：展开时就该把大小带上，不该事后再逐文件 getsize。"""
        ctx = make_ctx()
        folder = os.path.join(ctx._sandbox, "big")
        os.makedirs(os.path.join(folder, "sub"))
        with open(os.path.join(folder, "a.bin"), "wb") as fh:
            fh.write(b"x" * 1234)
        with open(os.path.join(folder, "sub", "b.bin"), "wb") as fh:
            fh.write(b"y" * 77)
        status, data = jcall(ctx, "POST", "/api/upload/enqueue",
                             {"localPaths": [folder], "cloudDir": "/cloud"})
        self.assertEqual(status, 200)
        sizes = {t["name"]: t["size"] for t in ctx._queue.list()}
        self.assertEqual(sizes["a.bin"], 1234)
        self.assertEqual(sizes["b.bin"], 77)

    def test_enqueue_skips_excluded_names(self):
        ctx = make_ctx()
        folder = os.path.join(ctx._sandbox, "mix")
        os.makedirs(folder)
        with open(os.path.join(folder, "keep.txt"), "w") as fh:
            fh.write("keep")
        with open(os.path.join(folder, ".DS_Store"), "w") as fh:
            fh.write("junk")
        status, data = jcall(ctx, "POST", "/api/upload/enqueue",
                             {"localPaths": [folder], "cloudDir": "/cloud"})
        self.assertEqual(status, 200)
        names = sorted(t["name"] for t in ctx._queue.list())
        self.assertEqual(names, ["keep.txt"])


class ExpandLocalDirTest(unittest.TestCase):
    """目录展开的边界：符号链接、非普通文件、嵌套相对路径。"""

    def test_follows_symlink_to_file(self):
        ctx = make_ctx()
        root = os.path.join(ctx._sandbox, "sym")
        os.makedirs(root)
        target = os.path.join(ctx._sandbox, "real.txt")
        with open(target, "w") as fh:
            fh.write("hello")
        link = os.path.join(root, "link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境不支持创建符号链接")
        if not os.path.islink(link):
            # Windows 上无权限时 os.symlink 会静默造一个普通空文件，
            # 这种环境下没有可验证对象（真机是 Linux，行为在这里不可测）
            self.skipTest("当前文件系统不支持真实符号链接（os.symlink 被静默降级）")
        items = http_api.expand_local_dir(root, "/cloud", [])
        self.assertEqual([i["remotePath"] for i in items], ["/cloud/sym/link.txt"])
        self.assertEqual(items[0]["size"], 5)

    def test_skips_non_regular_files(self):
        ctx = make_ctx()
        root = os.path.join(ctx._sandbox, "fifo")
        os.makedirs(root)
        fifo = os.path.join(root, "pipe")
        try:
            os.mkfifo(fifo)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境不支持 mkfifo")
        items = http_api.expand_local_dir(root, "/cloud", [])
        self.assertEqual(items, [])

    def test_nested_relative_paths(self):
        ctx = make_ctx()
        root = os.path.join(ctx._sandbox, "nest")
        os.makedirs(os.path.join(root, "x", "y"))
        with open(os.path.join(root, "x", "y", "deep.txt"), "w") as fh:
            fh.write("d")
        items = http_api.expand_local_dir(root, "/c", [])
        self.assertEqual([i["remotePath"] for i in items], ["/c/nest/x/y/deep.txt"])


class CompareTest(unittest.TestCase):
    def test_rejects_outside_whitelist(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/compare",
                             {"localRoot": "/etc", "cloudRoot": "/"})
        self.assertEqual(status, 400)
        self.assertIn("允许访问", data["error"])

    def test_compare_runs(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp")
        os.makedirs(d)
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("new file")
        # FakeClient 云端根目录里没有 new.txt → 应标为未上传
        status, data = jcall(ctx, "POST", "/api/compare",
                             {"localRoot": d, "cloudRoot": "/", "depth": 1})
        self.assertEqual(status, 200)
        r = data["result"]
        paths = [e["path"] for e in r["notUploaded"]]
        self.assertIn("new.txt", paths)

    def test_compare_returns_both_directions(self):
        """双向：既要有「本地多出」，也要有「云端多出」，界面才有得选。"""
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp2")
        os.makedirs(d)
        with open(os.path.join(d, "only-local.txt"), "w") as f:
            f.write("L")
        # 云端根目录里有 readme.txt，本地没有 → 云端多出
        status, data = jcall(ctx, "POST", "/api/compare",
                             {"localRoot": d, "cloudRoot": "/", "depth": 1})
        self.assertEqual(status, 200)
        r = data["result"]
        self.assertEqual([e["path"] for e in r["localExtra"]], ["only-local.txt"])
        self.assertIn("readme.txt", [e["path"] for e in r["cloudExtra"]])
        self.assertEqual(r["localExtraCount"], 1)
        self.assertGreaterEqual(r["cloudExtraCount"], 1)
        self.assertEqual([e["path"] for e in r["mismatch"]], [])

    def test_compare_returns_same_group(self):
        """「已同步」也要回明细 —— 界面的状态标签筛选靠它。

        只回计数的话，用户点「已同步」标签会看到一个空列表（或者说不出是哪几个
        文件已同步），这正是 v2.9.1 要解决的问题。
        """
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp3")
        os.makedirs(d)
        # 与云端根目录里的 readme.txt 同名同大小 → 已同步
        with open(os.path.join(d, "readme.txt"), "w") as f:
            f.write("x" * 12)
        status, data = jcall(ctx, "POST", "/api/compare",
                             {"localRoot": d, "cloudRoot": "/", "depth": 1})
        self.assertEqual(status, 200)
        r = data["result"]
        self.assertEqual([e["path"] for e in r["same"]], ["readme.txt"])
        self.assertEqual(r["sameCount"], 1)
        self.assertEqual(r["sameListed"], 1)
        self.assertFalse(r["uploadedTruncated"])
        # 已同步项不能被当成"多余"或"不一致"
        self.assertNotIn("readme.txt", [e["path"] for e in r["localExtra"]])
        self.assertNotIn("readme.txt", [e["path"] for e in r["cloudExtra"]])
        self.assertNotIn("readme.txt", [e["path"] for e in r["mismatch"]])

    # ---- v2.9.2：两侧各自可以是本地或云端 ----

    def test_two_sides_carry_their_own_kind(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp4")
        os.makedirs(d)
        with open(os.path.join(d, "only-local.txt"), "w") as f:
            f.write("L")
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": d},
            "right": {"side": "cloud", "path": "/"},
            "depth": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["left"], {"side": "local", "path": d})
        self.assertEqual(data["right"]["side"], "cloud")
        # 兼容字段仍在（升级期老调用方照样能读）
        self.assertEqual(data["localRoot"], d)
        self.assertEqual(data["cloudRoot"], "/")

    def test_cloud_to_cloud(self):
        """两侧都是云端 —— 左侧以前写死是本地目录，这种组合做不出来。"""
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/movies"},
            "right": {"side": "cloud", "path": "/"},
            "depth": 1})
        self.assertEqual(status, 200)
        r = data["result"]
        self.assertEqual([e["path"] for e in r["localExtra"]], [])
        self.assertIn("readme.txt", [e["path"] for e in r["cloudExtra"]])
        self.assertEqual(data["left"]["side"], "cloud")
        # 没有本地侧时 localRoot 给空；cloudRoot 给"是云端的那一侧"的路径
        self.assertEqual(data["localRoot"], "")
        self.assertEqual(data["cloudRoot"], "/movies")

    def test_left_cloud_right_local(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp5")
        os.makedirs(d)
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "cloud", "path": "/"},
            "right": {"side": "local", "path": d},
            "depth": 1})
        self.assertEqual(status, 200)
        # 云端根目录有 readme.txt、本地目录空 → 左侧独有
        self.assertIn("readme.txt", [e["path"] for e in data["result"]["localExtra"]])
        self.assertEqual(data["localRoot"], d)
        self.assertEqual(data["cloudRoot"], "/")

    def test_old_shape_still_accepted(self):
        """旧形状（localRoot / cloudRoot）继续可用 —— 老调用方不必跟着升级。"""
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp6")
        os.makedirs(d)
        status, data = jcall(ctx, "POST", "/api/compare",
                             {"localRoot": d, "cloudRoot": "/", "depth": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["left"]["side"], "local")
        self.assertEqual(data["right"]["side"], "cloud")

    def test_missing_cloud_dir_says_which_side(self):
        """报错必须指明是哪一侧 —— 否则用户不知道该改哪一个输入框。"""
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp7")
        os.makedirs(d)
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": d},
            "right": {"side": "cloud", "path": "/nope"},
            "depth": 1})
        self.assertEqual(status, 400)
        self.assertIn("右侧", data["error"])
        self.assertIn("不存在", data["error"])

    def test_cloud_side_not_a_directory(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp8")
        os.makedirs(d)
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": d},
            "right": {"side": "cloud", "path": "/readme.txt"},
            "depth": 1})
        self.assertEqual(status, 400)
        self.assertIn("不是目录", data["error"])

    def test_empty_local_side_says_which_side(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": ""},
            "right": {"side": "cloud", "path": "/"},
            "depth": 1})
        self.assertEqual(status, 400)
        self.assertIn("左侧", data["error"])

    def test_entries_carry_abs_path_and_owner(self):
        """条目要带「它在自己那一侧的绝对路径」。两侧都可能是云端之后，
        前端再按栏位拼路径就会拼错。"""
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp9")
        os.makedirs(d)
        with open(os.path.join(d, "L.txt"), "w") as f:
            f.write("L")
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": d},
            "right": {"side": "cloud", "path": "/"},
            "depth": 1})
        self.assertEqual(status, 200)
        r = data["result"]
        e = r["localExtra"][0]
        self.assertEqual(e["owner"], "left")
        self.assertEqual(e["absPath"], os.path.join(d, "L.txt"))
        c = [x for x in r["cloudExtra"] if x["path"] == "readme.txt"][0]
        self.assertEqual(c["owner"], "right")
        self.assertEqual(c["absPath"], "/readme.txt")

    def test_mismatch_carries_peer_abs_path(self):
        ctx = make_ctx()
        d = os.path.join(ctx._sandbox, "cmp10")
        os.makedirs(d)
        with open(os.path.join(d, "readme.txt"), "w") as f:
            f.write("x" * 3)          # 云端同名文件是 12 字节
        status, data = jcall(ctx, "POST", "/api/compare", {
            "left": {"side": "local", "path": d},
            "right": {"side": "cloud", "path": "/"},
            "depth": 1})
        self.assertEqual(status, 200)
        e = data["result"]["mismatch"][0]
        self.assertEqual(e["owner"], "left")
        self.assertEqual(e["absPath"], os.path.join(d, "readme.txt"))
        self.assertEqual(e["peerAbsPath"], "/readme.txt")


class SyncCopyTest(unittest.TestCase):
    """跨侧复制：云端↔云端 与 本地↔本地（本地↔云端仍走上传/下载队列）。"""

    def test_requires_side(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/sync/copy",
                             {"items": ["/a"], "target": "/b"})
        self.assertEqual(status, 400)
        self.assertIn("side", data["error"])

    def test_requires_target(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/sync/copy",
                             {"side": "cloud", "items": ["/a/x"]})
        self.assertEqual(status, 400)
        self.assertIn("target", data["error"])

    def test_requires_items(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/sync/copy",
                             {"side": "cloud", "items": [], "target": "/dst"})
        self.assertEqual(status, 400)
        self.assertIn("没有指定", data["error"])

    def test_cloud_copy_groups_by_parent(self):
        """同一父目录下的多个条目要合成一次请求 —— OpenList 的接口就是按
        src_dir + names 收的，逐条发等于白付网络往返。"""
        client = FakeClient()
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "cloud",
            "items": ["/src/a.mkv", "/src/b.mkv", "/other/c.mkv"],
            "target": "/dst"})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 3)
        self.assertEqual(len(client.copied), 2)
        by_src = dict((c["src"], c["names"]) for c in client.copied)
        self.assertEqual(sorted(by_src["/src"]), ["a.mkv", "b.mkv"])
        self.assertEqual(by_src["/other"], ["c.mkv"])
        self.assertTrue(all(c["dst"] == "/dst" for c in client.copied))
        self.assertTrue(all(c["overwrite"] for c in client.copied))

    def test_cloud_root_is_refused(self):
        client = FakeClient()
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "cloud", "items": ["/"], "target": "/dst"})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 0)
        self.assertEqual(len(data["failed"]), 1)
        self.assertEqual(client.copied, [])

    def test_cloud_group_failure_falls_back_one_by_one(self):
        """整组失败要降级逐条 —— 否则一个条目出错会让同组其它条目一起陪葬，
        而且报不出到底是谁失败。"""
        client = FakeClient()
        client.copy_fail = {"b.mkv"}
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "cloud",
            "items": ["/src/a.mkv", "/src/b.mkv"], "target": "/dst"})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 1)
        self.assertEqual([f["path"] for f in data["failed"]], ["/src/b.mkv"])

    def test_local_copy_target_outside_whitelist(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "local", "items": [ctx._sandbox], "target": "/etc"})
        self.assertEqual(status, 400)
        self.assertIn("允许访问", data["error"])

    def test_local_copy_file_and_tree(self):
        ctx = make_ctx()
        src = os.path.join(ctx._sandbox, "from")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("A")
        with open(os.path.join(src, "sub", "b.txt"), "w") as f:
            f.write("B")
        dst = os.path.join(ctx._sandbox, "to")
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "local", "items": [src], "target": dst})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 1, data)
        self.assertTrue(os.path.isfile(os.path.join(dst, "from", "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(dst, "from", "sub", "b.txt")))

    def test_local_copy_refuses_storage_root(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "local", "items": [ctx._sandbox], "target": ctx._sandbox})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 0)
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("根目录", data["failed"][0]["error"])

    def test_local_copy_same_dir_is_refused(self):
        ctx = make_ctx()
        sub = os.path.join(ctx._sandbox, "same")
        os.makedirs(sub)
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "local", "items": [sub], "target": sub})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 0)
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("就是目标目录", data["failed"][0]["error"])

    def test_local_copy_target_inside_source_is_refused(self):
        """目标落在源里面 → 复制会把自己套进自己，一路递归下去。"""
        ctx = make_ctx()
        src = os.path.join(ctx._sandbox, "outer")
        inner = os.path.join(src, "inner")
        os.makedirs(inner)
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("A")
        before = sorted(os.listdir(inner))
        status, data = jcall(ctx, "POST", "/api/sync/copy", {
            "side": "local", "items": [src], "target": inner})
        self.assertEqual(status, 200)
        self.assertEqual(data["copied"], 0)
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("内部", data["failed"][0]["error"])
        # 拒绝之后不该在目标里留下任何东西
        self.assertEqual(sorted(os.listdir(inner)), before)


class CloudRemoveTest(unittest.TestCase):
    def test_requires_paths(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/cloud/remove", {})
        self.assertEqual(status, 400)

    def test_removes_and_reports(self):
        client = FakeClient()
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/cloud/remove",
                             {"paths": ["/a/1.txt", "/b/2.txt"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 2)
        self.assertEqual(sorted(client.removed), ["/a/1.txt", "/b/2.txt"])

    def test_failures_are_returned_not_swallowed(self):
        client = FakeClient()
        client.removed_fail = ["/b/2.txt"]
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/cloud/remove",
                             {"paths": ["/a/1.txt", "/b/2.txt"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 1)
        self.assertEqual(len(data["failed"]), 1)
        self.assertEqual(data["failed"][0]["path"], "/b/2.txt")

    def test_writes_audit_log(self):
        """不可逆操作必须留痕：事后至少能回答"删了什么、什么时候删的"。"""
        client = FakeClient()
        ctx = make_ctx(client)
        jcall(ctx, "POST", "/api/cloud/remove", {"paths": ["/a/1.txt"]})
        path = os.path.join(config.data_dir(), "audit.log.jsonl")
        self.assertTrue(os.path.isfile(path))
        entry = json.loads(open(path, encoding="utf-8").read().strip().splitlines()[-1])
        self.assertEqual(entry["event"], "cloud_remove")
        self.assertEqual(entry["count"], 1)
        self.assertIn("/a/1.txt", entry["paths"])


class LocalRemoveTest(unittest.TestCase):
    def test_outside_whitelist_refused(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/local/remove", {"paths": ["/etc/passwd"]})
        self.assertEqual(status, 200)          # 接口本身成功，失败逐条回报
        self.assertEqual(data["removed"], 0)
        self.assertIn("允许访问", data["failed"][0]["error"])

    def test_deletes_file_inside_whitelist(self):
        ctx = make_ctx()
        victim = os.path.join(ctx._sandbox, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("bye")
        status, data = jcall(ctx, "POST", "/api/local/remove", {"paths": [victim]})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 1)
        self.assertFalse(os.path.exists(victim))

    def test_whitelist_root_itself_refused(self):
        """删掉存储空间根目录是灾难级误操作 —— 必须挡住。"""
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/local/remove", {"paths": [ctx._sandbox]})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 0)
        self.assertIn("根目录", data["failed"][0]["error"])
        self.assertTrue(os.path.isdir(ctx._sandbox))

    def test_missing_path_reported_not_fatal(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/local/remove",
                             {"paths": [os.path.join(ctx._sandbox, "nope.txt")]})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 0)
        self.assertEqual(len(data["failed"]), 1)

    def test_requires_paths(self):
        ctx = make_ctx()
        status, _ = jcall(ctx, "POST", "/api/local/remove", {"paths": []})
        self.assertEqual(status, 400)


class DownloadEnqueueTest(unittest.TestCase):
    def test_enqueue_files(self):
        ctx = make_ctx()
        local = os.path.join(ctx._sandbox, "out", "a.bin")
        status, data = jcall(ctx, "POST", "/api/download/enqueue",
                             {"items": [{"remotePath": "/src/a.bin",
                                         "localPath": local, "size": 5}]})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 1)
        task = ctx._downloads.list()[0]
        self.assertEqual(task["direction"], "download")
        self.assertEqual(task["name"], "a.bin")
        self.assertEqual(task["size"], 5)

    def test_rejects_local_outside_whitelist(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/download/enqueue",
                             {"items": [{"remotePath": "/src/a.bin",
                                         "localPath": "/etc/evil", "size": 1}]})
        self.assertEqual(status, 400)
        self.assertIn("允许访问", data["error"])

    def test_directory_is_expanded_recursively(self):
        """下载一个云端目录 → 服务端要先展开成文件清单。

        直接塞目录进队列只会得到一个"这是目录，不能直接下载"的失败任务。
        """
        client = FakeClient()
        client.tree["/season"] = [
            {"name": "ep1.mkv", "is_dir": False, "size": 10, "modified": ""},
            {"name": "sub", "is_dir": True, "size": 0, "modified": ""}]
        client.tree["/season/sub"] = [
            {"name": "ep2.mkv", "is_dir": False, "size": 20, "modified": ""}]
        ctx = make_ctx(client)
        local = os.path.join(ctx._sandbox, "dl", "season")
        status, data = jcall(ctx, "POST", "/api/download/enqueue",
                             {"items": [{"remotePath": "/season", "localPath": local,
                                         "isDir": True}]})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 2)
        remotes = sorted(t["remotePath"] for t in ctx._downloads.list())
        self.assertEqual(remotes, ["/season/ep1.mkv", "/season/sub/ep2.mkv"])
        locals_ = sorted(t["localPath"] for t in ctx._downloads.list())
        self.assertEqual(locals_, sorted([os.path.join(local, "ep1.mkv"),
                                          os.path.join(local, "sub", "ep2.mkv")]))

    def test_empty_dir_reports_clearly(self):
        client = FakeClient()
        client.tree["/empty"] = []
        ctx = make_ctx(client)
        status, data = jcall(ctx, "POST", "/api/download/enqueue",
                             {"items": [{"remotePath": "/empty",
                                         "localPath": os.path.join(ctx._sandbox, "e"),
                                         "isDir": True}]})
        self.assertEqual(status, 400)
        self.assertIn("没有可下载", data["error"])

    def test_requires_items(self):
        ctx = make_ctx()
        status, _ = jcall(ctx, "POST", "/api/download/enqueue", {"items": []})
        self.assertEqual(status, 400)


class TransferListTest(unittest.TestCase):
    """传输列表要把上传、下载、绑定三类任务合成一屏，并标明方向。"""

    def test_download_tasks_appear_with_direction(self):
        ctx = make_ctx()
        local = os.path.join(ctx._sandbox, "x.bin")
        jcall(ctx, "POST", "/api/download/enqueue",
              {"items": [{"remotePath": "/s/x.bin", "localPath": local, "size": 3}]})
        status, data = jcall(ctx, "GET", "/api/upload/list")
        self.assertEqual(status, 200)
        downs = [t for t in data["tasks"] if t.get("direction") == "download"]
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0]["remotePath"], "/s/x.bin")

    def test_download_id_routes_to_download_queue(self):
        """按 id 操作必须先判断它属于哪条队列 —— 打错只会得到"任务不存在"。"""
        ctx = make_ctx()
        local = os.path.join(ctx._sandbox, "y.bin")
        jcall(ctx, "POST", "/api/download/enqueue",
              {"items": [{"remotePath": "/s/y.bin", "localPath": local, "size": 3}]})
        tid = ctx._downloads.list()[0]["id"]

        status, data = jcall(ctx, "POST", "/api/upload/cancel", {"id": tid})
        self.assertEqual(status, 200)
        self.assertEqual(ctx._downloads.get(tid)["status"], "cancelled")

        status, _ = jcall(ctx, "POST", "/api/upload/cancel", {"id": "nope"})
        self.assertEqual(status, 404)

        status, data = jcall(ctx, "POST", "/api/upload/retry", {"id": tid})
        self.assertEqual(status, 200)
        self.assertEqual(ctx._downloads.get(tid)["status"], "pending")

        status, _ = jcall(ctx, "POST", "/api/upload/remove", {"id": tid})
        self.assertEqual(status, 200)
        self.assertEqual(ctx._downloads.list(), [])

    def test_pause_covers_both_queues(self):
        ctx = make_ctx()
        jcall(ctx, "POST", "/api/upload/pause", {"paused": True})
        self.assertTrue(ctx._queue.is_paused())
        self.assertTrue(ctx._downloads.is_paused())
        jcall(ctx, "POST", "/api/upload/pause", {"paused": False})
        self.assertFalse(ctx._downloads.is_paused())

    def test_clear_done_covers_both_queues(self):
        ctx = make_ctx()
        local = os.path.join(ctx._sandbox, "z.bin")
        jcall(ctx, "POST", "/api/download/enqueue",
              {"items": [{"remotePath": "/s/z.bin", "localPath": local, "size": 3}]})
        tid = ctx._downloads.list()[0]["id"]
        ctx._downloads._set({"id": tid}, status="done")
        ctx._downloads.save(force=True)
        status, data = jcall(ctx, "POST", "/api/upload/clear", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 1)


class ConfigTest(unittest.TestCase):
    def test_get_set(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("uploadConcurrency", data["config"])
        status, data = jcall(ctx, "POST", "/api/config",
                             {"uploadConcurrency": 4, "skipExisting": False})
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["uploadConcurrency"], 4)
        self.assertFalse(data["config"]["skipExisting"])


class BodyLimitTest(unittest.TestCase):
    def test_oversized_body_closes_connection(self):
        ctx = make_ctx()
        big = {"rootId": "x" * (http_api.MAX_BODY_BYTES + 10)}
        status, headers, raw = call(ctx, "POST", "/api/config", body=big)
        self.assertEqual(status, 413)
        self.assertEqual(headers.get("connection"), "close",
                         "拒绝超大请求体必须关连接，否则 keep-alive 错位")


class RetryAllRouteTest(unittest.TestCase):
    """v2.1.0：/api/upload/retry 支持 {"all": true} 一键重试。"""

    def _seed_failed(self, ctx):
        f = os.path.join(ctx._sandbox, "fail.txt")
        with open(f, "w") as fh:
            fh.write("data")
        ctx._queue.add([{"localPath": f, "remotePath": "/fail.txt"}])
        tid = ctx._queue.list()[0]["id"]
        ctx._queue._set({"id": tid}, status="failed", attempts=3, error="boom")
        ctx._queue.save(force=True)
        return tid

    def test_retry_all(self):
        ctx = make_ctx()
        tid = self._seed_failed(ctx)
        status, data = jcall(ctx, "POST", "/api/upload/retry", {"all": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["retried"], 1)
        task = ctx._queue.get(tid)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["error"], "")

    def test_retry_all_none_failed(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/upload/retry", {"all": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["retried"], 0)

    def test_retry_single_still_404_when_missing(self):
        """单条分支行为保持不变：任务不存在仍 404。"""
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/upload/retry", {"id": "nope"})
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])

    def test_retry_single_still_works(self):
        ctx = make_ctx()
        tid = self._seed_failed(ctx)
        status, data = jcall(ctx, "POST", "/api/upload/retry", {"id": tid})
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "pending")


class RetryAllIntegrationTest(unittest.TestCase):
    """跨层：路由层 {all:true} → 队列 retry_all，混合状态队列只动 failed。"""

    def test_retry_all_mixed_queue_via_http(self):
        ctx = make_ctx()
        items = []
        for i in range(4):
            f = os.path.join(ctx._sandbox, "mix%d.txt" % i)
            with open(f, "w") as fh:
                fh.write("x" * (i + 1))
            items.append({"localPath": f, "remotePath": "/mix%d.txt" % i})
        ctx._queue.add(items)
        tasks = ctx._queue.list()          # 保持插入顺序
        ctx._queue._set({"id": tasks[0]["id"]}, status="failed",
                        attempts=3, error="boom", uploaded=5)
        ctx._queue._set({"id": tasks[1]["id"]}, status="done", uploaded=1)
        ctx._queue._set({"id": tasks[2]["id"]}, status="cancelled")
        ctx._queue.save(force=True)

        status, data = jcall(ctx, "POST", "/api/upload/retry", {"all": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["retried"], 1)

        failed = ctx._queue.get(tasks[0]["id"])
        self.assertEqual(failed["status"], "pending")
        self.assertEqual(failed["attempts"], 0)
        self.assertEqual(failed["error"], "")
        self.assertEqual(failed["uploaded"], 5, "HTTP 路径同样要保留 uploaded")
        # 非 failed 一个都不许变
        self.assertEqual(ctx._queue.get(tasks[1]["id"])["status"], "done")
        self.assertEqual(ctx._queue.get(tasks[2]["id"])["status"], "cancelled")
        self.assertEqual(ctx._queue.get(tasks[3]["id"])["status"], "pending")


class DepthValidationTest(unittest.TestCase):
    """depth 曾经直接 int(body["depth"])：填错就 500，"不限"(-1) 还会被
    verify 的 max(0,...) 悄悄变成 0（只看当前层）。"""

    def setUp(self):
        self.ctx = make_ctx()
        self.d = self.ctx._sandbox
        with open(os.path.join(self.d, "a.txt"), "w") as fh:
            fh.write("x")

    def test_negative_depth_means_unlimited(self):
        status, data = jcall(self.ctx, "POST", "/api/compare",
                             {"localRoot": self.d, "cloudRoot": "/", "depth": -1})
        self.assertEqual(status, 200)
        self.assertEqual(data["depth"], http_api.DEPTH_UNLIMITED)

    def test_non_numeric_depth_is_400_not_500(self):
        status, data = jcall(self.ctx, "POST", "/api/compare",
                             {"localRoot": self.d, "cloudRoot": "/", "depth": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("深度", data["error"])

    def test_depth_defaults_to_config(self):
        status, data = jcall(self.ctx, "POST", "/api/compare",
                             {"localRoot": self.d, "cloudRoot": "/"})
        self.assertEqual(status, 200)
        self.assertEqual(data["depth"], config.load_config()["scanDepth"])

    def test_resolve_depth_directly(self):
        self.assertEqual(http_api.resolve_depth(None, 4), 4)
        self.assertEqual(http_api.resolve_depth("", 4), 4)
        self.assertEqual(http_api.resolve_depth("0"), 0)
        self.assertEqual(http_api.resolve_depth(-5), http_api.DEPTH_UNLIMITED)


class ConfigSanitizeTest(unittest.TestCase):
    """配置写入曾经完全不校验：并发能填 999、深度能填字符串、
    excludeNames 能填成字符串结果按字符逐个匹配。"""

    def setUp(self):
        self.ctx = make_ctx()

    def test_concurrency_clamped_to_range(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"uploadConcurrency": 999})
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["uploadConcurrency"], 8)
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"uploadConcurrency": 0})
        self.assertEqual(data["config"]["uploadConcurrency"], 1)

    def test_max_attempts_clamped(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"maxAttempts": 999})
        self.assertEqual(data["config"]["maxAttempts"], 10)

    def test_bad_scan_depth_falls_back(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"scanDepth": "abc"})
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["scanDepth"],
                         config.DEFAULT_CONFIG["scanDepth"])

    def test_exclude_names_string_becomes_list(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"excludeNames": "a.tmp, b.log"})
        self.assertEqual(data["config"]["excludeNames"], ["a.tmp", "b.log"])

    def test_exclude_names_garbage_falls_back_to_default(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"excludeNames": 12345})
        self.assertEqual(data["config"]["excludeNames"],
                         list(config.DEFAULT_CONFIG["excludeNames"]))

    def test_auto_scan_interval_has_floor(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"autoScanInterval": 1})
        self.assertGreaterEqual(data["config"]["autoScanInterval"], 30)

    def test_local_source_mode_whitelisted(self):
        status, data = jcall(self.ctx, "POST", "/api/config",
                             {"localSourceMode": "evil"})
        self.assertEqual(data["config"]["localSourceMode"], "fs")

    def test_public_config_hides_unknown_keys(self):
        cfg = config.load_config()
        cfg["internalSecretToken"] = "do-not-leak"
        config.save_config(cfg)
        status, data = jcall(self.ctx, "GET", "/api/config")
        self.assertNotIn("internalSecretToken", data["config"])
        self.assertIn("uploadConcurrency", data["config"])


class UploadParamTypeTest(unittest.TestCase):
    """relPaths / targetIds 传错类型会被当成子串匹配，静默提交错文件。"""

    def setUp(self):
        self.ctx = make_ctx()
        self.store = self.ctx.bindings()
        self.b = self.store.create_binding({
            "localPath": "/source", "cloudTargets": ["/backup"]})

    def test_rel_paths_must_be_list(self):
        status, data = jcall(self.ctx, "POST", "/api/bindings/upload",
                             {"id": self.b["id"], "relPaths": "a.txt"})
        self.assertEqual(status, 400)
        self.assertIn("数组", data["error"])

    def test_target_ids_must_be_list(self):
        status, data = jcall(self.ctx, "POST", "/api/bindings/upload",
                             {"id": self.b["id"], "targetIds": "x"})
        self.assertEqual(status, 400)
        self.assertIn("数组", data["error"])


class QueueRouteTest(unittest.TestCase):
    def test_remove_unknown_task_is_404(self):
        ctx = make_ctx()
        status, data = jcall(ctx, "POST", "/api/upload/remove", {"id": "nope"})
        self.assertEqual(status, 404)

    def test_pause_survives_queue_reset(self):
        """改配置会重建队列；重建后不能把用户的「暂停」丢掉。"""
        ctx = make_ctx()
        jcall(ctx, "POST", "/api/upload/pause", {"paused": True})
        self.assertTrue(ctx.queue().is_paused())
        ctx.reset_queue()
        self.assertTrue(ctx.queue().is_paused(),
                        "重建队列后暂停状态必须保留")

    def test_resume_after_reset(self):
        ctx = make_ctx()
        jcall(ctx, "POST", "/api/upload/pause", {"paused": True})
        jcall(ctx, "POST", "/api/upload/pause", {"paused": False})
        ctx.reset_queue()
        self.assertFalse(ctx.queue().is_paused())


class CloudListRequestCountTest(unittest.TestCase):
    def test_single_list_call_per_directory(self):
        """列一次目录只应发一轮 fs/list（write 由 meta 顺带带回）。"""
        client = FakeClient()
        client.tree = {"/": [{"name": "a.txt", "is_dir": False, "size": 1}]}
        ctx = make_ctx(client)
        calls = []
        orig_list_dir = client.list_dir

        def counting_list_dir(path, page=1, per_page=1000):
            calls.append((path, page))
            return orig_list_dir(path, page=page, per_page=per_page)

        client.list_dir = counting_list_dir
        status, data = jcall(ctx, "GET", "/api/cloud/list?path=/")
        self.assertEqual(status, 200)
        self.assertTrue(data["write"], "write 标志要能从 meta 拿到")
        self.assertEqual(len(calls), 1, "不该为了 write 再发一次请求")


if __name__ == "__main__":
    unittest.main(verbosity=2)
