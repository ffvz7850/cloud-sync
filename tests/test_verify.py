# -*- coding: utf-8 -*-
"""verify：本地 vs 云端对比的四分类语义，以及待上传清单收敛。

核心是「标出尚未上传的本地文件」，方向绝不能搞反（旧版是列云端多余项）。
"""
import os
import unittest

import support  # noqa: F401  负责 sys.path 与环境隔离

import verify


def L(entries):
    """构造本地 map：{rel: {isDir,size,mtime}}"""
    out = {}
    for rel, is_dir, size in entries:
        out[rel] = {"isDir": is_dir, "size": size, "mtime": 1000}
    return out


def C(entries):
    """构造云端 map：{rel: {name,isDir,size,absPath}}"""
    out = {}
    for rel, is_dir, size in entries:
        out[rel] = {"name": rel.split("/")[-1], "isDir": is_dir,
                    "size": size, "modified": "", "absPath": "/" + rel}
    return out


class CompareTest(unittest.TestCase):
    def test_not_uploaded_is_local_only(self):
        """本地有、云端没有 → notUploaded（这是用户最关心的）。"""
        result = verify.compare(
            L([("a.txt", False, 10), ("b.txt", False, 20)]),
            C([("a.txt", False, 10)]))
        self.assertEqual([e["path"] for e in result["notUploaded"]], ["b.txt"])
        self.assertEqual(result["uploadedFiles"], 1)

    def test_size_mismatch_detected(self):
        """两边都有但大小不同 → sizeMismatch（云端是旧版本，需重传）。"""
        result = verify.compare(
            L([("a.txt", False, 100)]),
            C([("a.txt", False, 50)]))
        self.assertEqual(len(result["sizeMismatch"]), 1)
        self.assertEqual(result["sizeMismatch"][0]["cloudSize"], 50)
        self.assertEqual(result["uploadedFiles"], 0)

    def test_same_size_is_uploaded(self):
        result = verify.compare(
            L([("a.txt", False, 100)]),
            C([("a.txt", False, 100)]))
        self.assertEqual(result["uploadedFiles"], 1)
        self.assertEqual(result["notUploaded"], [])
        self.assertEqual(result["sizeMismatch"], [])

    def test_dir_only_compares_existence(self):
        """目录只比存在性，不比大小（目录没有大小概念）。"""
        result = verify.compare(
            L([("dir", True, 0), ("dir/a.txt", False, 5)]),
            C([("dir", True, 0), ("dir/a.txt", False, 5)]))
        self.assertEqual(result["uploadedDirs"], 1)
        self.assertEqual(result["uploadedFiles"], 1)
        self.assertEqual(result["notUploaded"], [])

    def test_cloud_only_listed_but_no_action(self):
        """云端独有仅展示，不算未上传，也不进待传清单。"""
        result = verify.compare(
            L([("a.txt", False, 1)]),
            C([("a.txt", False, 1), ("zzz.txt", False, 9)]))
        self.assertEqual([e["path"] for e in result["cloudOnly"]], ["zzz.txt"])
        self.assertEqual(result["notUploaded"], [])

    def test_pending_bytes_only_files(self):
        result = verify.compare(
            L([("d", True, 0), ("d/a", False, 10), ("b", False, 20)]),
            C([]))
        # 两个文件未上传：10 + 20；目录 d 不算字节
        self.assertEqual(result["pendingBytes"], 30)

    def test_empty_both_sides(self):
        result = verify.compare(L([]), C([]))
        self.assertEqual(result["notUploaded"], [])
        self.assertEqual(result["localTotal"], 0)


class SplitDiffTest(unittest.TestCase):
    """面向操作的「顶层差异」：目录已列出就不能再单列它的子项。

    不收敛的三个实际后果：① 批量删除会把同一棵子树删两遍（第二遍必然报不存在）；
    ② 批量下载/上传把整棵树重复入队，任务列表里几十倍冗余；
    ③ 界面上同一个目录既显示"整目录"又逐个列子文件，用户根本不知道该点哪个。
    """

    def _result(self):
        return verify.compare(
            L([("localdir", True, 0), ("localdir/a.txt", False, 10),
               ("localdir/sub", True, 0), ("localdir/sub/b.txt", False, 20),
               ("keep.txt", False, 5)]),
            C([("keep.txt", False, 5), ("clouddir", True, 0),
               ("clouddir/c.txt", False, 30), ("clouddir/deep", True, 0),
               ("clouddir/deep/d.txt", False, 40)]))

    def test_local_extra_is_top_level_only(self):
        r = self._result()
        self.assertEqual([e["path"] for e in r["localExtra"]], ["localdir"])
        # 全量列表仍然保留（诊断用），只是不拿它做操作
        self.assertIn("localdir/a.txt", [e["path"] for e in r["notUploaded"]])

    def test_cloud_extra_is_top_level_only(self):
        r = self._result()
        self.assertEqual([e["path"] for e in r["cloudExtra"]], ["clouddir"])

    def test_counts_and_bytes(self):
        r = self._result()
        self.assertEqual(r["localExtraCount"], 1)
        self.assertEqual(r["cloudExtraCount"], 1)
        # 顶层目录本身不计字节（文件在它下面，重复计就是虚高）
        self.assertEqual(r["localExtraBytes"], 0)
        self.assertEqual(r["cloudExtraBytes"], 0)

    def test_file_level_extras_are_kept(self):
        r = verify.compare(L([("a.txt", False, 1)]), C([("b.txt", False, 2)]))
        self.assertEqual([e["path"] for e in r["localExtra"]], ["a.txt"])
        self.assertEqual([e["path"] for e in r["cloudExtra"]], ["b.txt"])
        self.assertEqual(r["localExtraBytes"], 1)
        self.assertEqual(r["cloudExtraBytes"], 2)

    def test_mismatch_is_not_part_of_extras(self):
        """大小不一致两边都有内容 —— 它绝不能落进"可清除"的多余项里。"""
        r = verify.compare(L([("a.txt", False, 10)]), C([("a.txt", False, 5)]))
        self.assertEqual(r["localExtra"], [])
        self.assertEqual(r["cloudExtra"], [])
        self.assertEqual([e["path"] for e in r["mismatch"]], ["a.txt"])

    def test_no_duplicate_delete_targets(self):
        """清除集合里的路径必须是"互不包含"的，否则会删同一个子树两次。"""
        r = self._result()
        targets = [e["path"] for e in r["localExtra"] + r["cloudExtra"]]
        for path in targets:
            for other in targets:
                if path == other:
                    continue
                self.assertFalse(other.startswith(path + "/"),
                                 "%s 与其后代 %s 同时出现在清除集合里" % (path, other))

    def test_prune_empty(self):
        self.assertEqual(verify._prune_descendants([]), [])
        self.assertEqual(verify._prune_descendants(None), [])


class SameGroupTest(unittest.TestCase):
    """「已同步」组是**只读**视图，前端的标签筛选就靠它。

    两条刻意的规则：
    ① 只列文件 —— 目录的"已同步"只代表两边都存在这个路径，不代表里面传完了
       （子文件可能整批还没上传），列出来会让用户以为已经齐了；
    ② 不归并 —— `_prune_descendants` 是为"批量去重"服务的，已同步组不参与任何
       批量操作，套上归并只会让明明同步了的子文件凭空消失。
    """

    def test_files_are_listed(self):
        r = verify.compare(L([("a.txt", False, 10), ("b.txt", False, 20)]),
                           C([("a.txt", False, 10), ("b.txt", False, 20)]))
        self.assertEqual([e["path"] for e in r["same"]], ["a.txt", "b.txt"])
        self.assertEqual(r["sameCount"], 2)
        self.assertEqual(r["sameListed"], 2)
        self.assertEqual(r["uploadedTruncated"], False)

    def test_dirs_are_counted_but_not_listed(self):
        r = verify.compare(L([("sub", True, 0), ("sub/a.txt", False, 10)]),
                           C([("sub", True, 0), ("sub/a.txt", False, 10)]))
        self.assertEqual([e["path"] for e in r["same"]], ["sub/a.txt"])
        self.assertEqual(r["uploadedDirs"], 1)
        self.assertEqual(r["sameCount"], 1)

    def test_children_are_not_pruned(self):
        """不归并：子文件必须全部留下（归并是批量操作用的，这里会丢信息）。"""
        r = verify.compare(
            L([("sub", True, 0), ("sub/a.txt", False, 1), ("sub/b.txt", False, 2)]),
            C([("sub", True, 0), ("sub/a.txt", False, 1), ("sub/b.txt", False, 2)]))
        self.assertEqual([e["path"] for e in r["same"]], ["sub/a.txt", "sub/b.txt"])

    def test_only_identical_files(self):
        """大小不一致的、单边多出的，都不能混进已同步。"""
        r = verify.compare(
            L([("same.txt", False, 1), ("diff.txt", False, 9), ("only.txt", False, 3)]),
            C([("same.txt", False, 1), ("diff.txt", False, 4)]))
        self.assertEqual([e["path"] for e in r["same"]], ["same.txt"])
        self.assertEqual(r["mismatchCount"], 1)
        self.assertEqual(r["localExtraCount"], 1)

    def test_truncation_keeps_full_count(self):
        """明细被截断时计数必须仍是全量真值 —— 否则界面徽标会跟着变小。"""
        old = verify.UPLOADED_LIMIT
        verify.UPLOADED_LIMIT = 3
        try:
            entries = [("f%d.txt" % i, False, 1) for i in range(10)]
            r = verify.compare(L(entries), C(entries))
        finally:
            verify.UPLOADED_LIMIT = old
        self.assertEqual(r["uploadedTruncated"], True)
        self.assertEqual(len(r["same"]), 3)          # 明细按上限截断
        self.assertEqual(r["sameCount"], 10)         # 计数不受影响
        self.assertEqual(r["sameListed"], 3)
        self.assertEqual(r["uploadedFiles"], 10)
        self.assertEqual(len(r["notUploaded"]), 0)

    def test_no_same_bytes_field(self):
        """截断后按剩余明细求和是错的，所以干脆不给这个字段。"""
        r = verify.compare(L([("a.txt", False, 10)]), C([("a.txt", False, 10)]))
        self.assertNotIn("sameBytes", r)

    def test_same_is_readonly_not_a_diff(self):
        """已同步既不是"多余项"也不是"不一致"，绝不能混进可清除/可覆盖的集合。"""
        r = verify.compare(L([("a.txt", False, 10)]), C([("a.txt", False, 10)]))
        self.assertEqual(r["localExtra"], [])
        self.assertEqual(r["cloudExtra"], [])
        self.assertEqual(r["mismatch"], [])

    def test_split_diff_alone_falls_back_to_list_length(self):
        """单独调 split_diff（不经 compare）时没有全量计数，退化成明细条数。"""
        out = verify.split_diff({"uploaded": [{"path": "x", "isDir": False, "size": 1}]})
        self.assertEqual(out["sameCount"], 1)
        self.assertEqual(out["sameListed"], 1)


class EffectiveExcludesTest(unittest.TestCase):
    """自家临时文件必须由代码兜底，不能只靠用户配置。"""

    def test_ddpart_always_excluded(self):
        self.assertTrue(verify.match_exclude("a.bin.ddpart", verify.effective_excludes([])))
        self.assertTrue(verify.match_exclude("a.bin.ddpart",
                                             verify.effective_excludes(["*.tmp"])))

    def test_user_patterns_survive(self):
        merged = verify.effective_excludes(["*.log"])
        self.assertTrue(verify.match_exclude("x.log", merged))
        self.assertTrue(verify.match_exclude("x.ddpart", merged))

    def test_scan_local_skips_partial_download(self):
        """中断的下载留下 .ddpart，不能让它在下次比对里变成"本地多出的文件"。"""
        import tempfile
        root = tempfile.mkdtemp(prefix="dd-part-")
        with open(os.path.join(root, "done.bin"), "wb") as fh:
            fh.write(b"x" * 10)
        with open(os.path.join(root, "half.bin.ddpart"), "wb") as fh:
            fh.write(b"y" * 3)
        m, _ = verify.scan_local(root, depth=1, excludes=[])   # 用户把排除清空也一样
        self.assertIn("done.bin", m)
        self.assertNotIn("half.bin.ddpart", m)


class PickUploadTargetsTest(unittest.TestCase):
    def _result(self):
        return verify.compare(
            L([("a.txt", False, 10), ("dir", True, 0), ("dir/b.txt", False, 20),
               ("c.txt", False, 30)]),
            C([("a.txt", False, 10)]))

    def test_default_takes_all_pending_files(self):
        """默认取全部待上传的**文件**（目录在上传时按需创建，不单独成任务）。"""
        targets = verify.pick_upload_targets(self._result())
        paths = [t["path"] for t in targets]
        self.assertIn("dir/b.txt", paths)
        self.assertIn("c.txt", paths)
        self.assertNotIn("a.txt", paths)      # 已上传
        self.assertNotIn("dir", paths)        # 目录不进任务

    def test_selected_subset(self):
        targets = verify.pick_upload_targets(self._result(), ["c.txt"])
        self.assertEqual([t["path"] for t in targets], ["c.txt"])

    def test_selected_ignores_unknown(self):
        targets = verify.pick_upload_targets(self._result(), ["nope.txt", "c.txt"])
        self.assertEqual([t["path"] for t in targets], ["c.txt"])


class ScanLocalTest(unittest.TestCase):
    def test_scan_and_exclude(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dd-scan-")
        os.makedirs(os.path.join(root, "sub"))
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(root, "sub", "b.mkv"), "wb") as f:
            f.write(b"x" * 100)
        with open(os.path.join(root, ".DS_Store"), "w") as f:
            f.write("junk")
        m, errors = verify.scan_local(root, depth=2)
        self.assertIn("a.txt", m)
        self.assertIn("sub", m)
        self.assertIn("sub/b.mkv", m)
        self.assertNotIn(".DS_Store", m)      # 默认排除
        self.assertEqual(m["a.txt"]["size"], 5)
        self.assertEqual(m["sub/b.mkv"]["size"], 100)

    def test_missing_dir_raises(self):
        with self.assertRaises(ValueError):
            verify.scan_local("/nonexistent-xyz-123", depth=1)

    def test_nodes_carry_abs_path(self):
        """云端扫描一直带 absPath，本地也必须带 —— 否则"两侧同构"就断了：
        左侧也可能是云端之后，前端没法再用「本地 root + 相对路径」拼出路径。"""
        import tempfile
        root = tempfile.mkdtemp(prefix="dd-scan-abs-")
        os.makedirs(os.path.join(root, "sub"))
        with open(os.path.join(root, "sub", "b.txt"), "w") as f:
            f.write("B")
        m, _ = verify.scan_local(root, depth=2)
        self.assertEqual(m["sub"]["absPath"], os.path.join(root, "sub"))
        self.assertEqual(m["sub/b.txt"]["absPath"],
                         os.path.join(root, "sub", "b.txt"))


class SideMetaTest(unittest.TestCase):
    """两侧都可能是云端之后，每个条目必须能自报「它在自己那一侧的绝对路径」。

    以前这是前端的活儿（本地 root + 相对路径拼一下）。现在拼不出来了：
    左侧也可能是云端，那条规则会把云端路径拼成本地路径。
    """

    @staticmethod
    def _with_abs(entries, prefix):
        out = {}
        for rel, is_dir, size in entries:
            out[rel] = {"isDir": is_dir, "size": size, "mtime": 0,
                        "absPath": prefix + "/" + rel}
        return out

    def test_left_entries_carry_abs_path_and_owner(self):
        r = verify.compare(self._with_abs([("a.txt", False, 10)], "/left"),
                           C([("b.txt", False, 5)]))
        e = r["localExtra"][0]
        self.assertEqual(e["owner"], "left")
        self.assertEqual(e["absPath"], "/left/a.txt")

    def test_right_entries_carry_abs_path_and_owner(self):
        r = verify.compare(L([]), C([("b.txt", False, 5)]))
        e = r["cloudExtra"][0]
        self.assertEqual(e["owner"], "right")
        self.assertEqual(e["absPath"], "/b.txt")

    def test_mismatch_carries_both_sides(self):
        r = verify.compare(self._with_abs([("a.txt", False, 10)], "/left"),
                           C([("a.txt", False, 99)]))
        e = r["mismatch"][0]
        self.assertEqual(e["owner"], "left")
        self.assertEqual(e["absPath"], "/left/a.txt")
        self.assertEqual(e["peerAbsPath"], "/a.txt")
        self.assertEqual(e["cloudSize"], 99)

    def test_missing_abs_path_stays_empty(self):
        """拿不到就留空 —— 编一个出来比留空更糟（用户会照着它去删别的东西）。"""
        r = verify.compare(L([("a.txt", False, 10)]), C([]))
        self.assertEqual(r["localExtra"][0]["absPath"], "")

    def test_prune_keeps_meta_fields(self):
        r = verify.compare(
            self._with_abs([("d", True, 0), ("d/x.txt", False, 1)], "/left"),
            C([]))
        self.assertEqual([e["path"] for e in r["localExtra"]], ["d"])
        self.assertEqual(r["localExtra"][0]["absPath"], "/left/d")
        self.assertEqual(r["localExtra"][0]["owner"], "left")


class ScanCloudTest(unittest.TestCase):
    def test_recursive_walk(self):
        """用 fake client 验证云端递归扫描与相对路径计算。"""
        tree = {
            "/": [{"name": "dir", "is_dir": True, "size": 0},
                  {"name": "top.txt", "is_dir": False, "size": 7}],
            "/dir": [{"name": "inner.txt", "is_dir": False, "size": 3}],
        }

        class FakeClient:
            def list_all(self, path):
                return tree.get(path, [])

        m = verify.scan_cloud(FakeClient(), "/", depth=2)
        self.assertIn("dir", m)
        self.assertIn("top.txt", m)
        self.assertIn("dir/inner.txt", m)
        self.assertEqual(m["dir/inner.txt"]["size"], 3)
        self.assertEqual(m["dir/inner.txt"]["absPath"], "/dir/inner.txt")

    def test_depth_limit(self):
        tree = {
            "/": [{"name": "d1", "is_dir": True, "size": 0}],
            "/d1": [{"name": "d2", "is_dir": True, "size": 0}],
            "/d1/d2": [{"name": "deep.txt", "is_dir": False, "size": 1}],
        }

        class FakeClient:
            def list_all(self, path):
                return tree.get(path, [])

        shallow = verify.scan_cloud(FakeClient(), "/", depth=0)
        self.assertIn("d1", shallow)
        self.assertNotIn("d1/d2", shallow)

        deep = verify.scan_cloud(FakeClient(), "/", depth=3)
        self.assertIn("d1/d2/deep.txt", deep)


class ScanCloudConcurrencyTest(unittest.TestCase):
    """云端扫描必须**真的并发**，且并发不改变结果。"""

    def _wide_tree(self, dirs=8, files_per_dir=2):
        root = [{"name": "d%d" % i, "is_dir": True, "size": 0} for i in range(dirs)]
        tree = {"/": root}
        for i in range(dirs):
            tree["/d%d" % i] = [{"name": "f%d.txt" % j, "is_dir": False, "size": 10 + j}
                                for j in range(files_per_dir)]
        return tree

    def test_lists_in_parallel(self):
        """同一层的多个目录要同时在列，而不是一个一个排队。"""
        import threading
        import time
        tree = self._wide_tree()
        state = {"now": 0, "peak": 0}
        lock = threading.Lock()

        class SlowClient(object):
            def list_all(self, path, meta=None):
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                time.sleep(0.03)
                with lock:
                    state["now"] -= 1
                return tree.get(path, [])

        m = verify.scan_cloud(SlowClient(), "/", depth=1, concurrency=4)
        self.assertEqual(len(m), 8 * 3)          # 8 个目录 + 16 个文件
        self.assertGreater(state["peak"], 1,
                           "并发数为 4 时同时进行的请求应大于 1，否则说明仍是串行")

    def test_concurrency_does_not_change_result(self):
        tree = self._wide_tree(dirs=6, files_per_dir=3)

        class FakeClient(object):
            def list_all(self, path, meta=None):
                return tree.get(path, [])

        serial = verify.scan_cloud(FakeClient(), "/", depth=2, concurrency=1)
        parallel = verify.scan_cloud(FakeClient(), "/", depth=2, concurrency=8)
        self.assertEqual(serial, parallel)

    def test_listing_error_still_raises(self):
        """某个目录列不出来时必须抛错，不能悄悄少扫一层（会误判成"未上传"）。"""
        tree = {"/": [{"name": "bad", "is_dir": True, "size": 0}]}

        class BrokenClient(object):
            def list_all(self, path, meta=None):
                if path == "/bad":
                    raise RuntimeError("permission denied")
                return tree.get(path, [])

        with self.assertRaises(RuntimeError) as cm:
            verify.scan_cloud(BrokenClient(), "/", depth=2)
        self.assertIn("bad", str(cm.exception))


class MapConcurrentTest(unittest.TestCase):
    def test_preserves_input_order(self):
        out = verify.map_concurrent(lambda x: x * 10, [3, 1, 2], 4)
        self.assertEqual(out, [30, 10, 20])

    def test_collects_exceptions_in_place(self):
        def fn(x):
            if x == 2:
                raise ValueError("boom")
            return x
        out = verify.map_concurrent(fn, [1, 2, 3], 3)
        self.assertEqual(out[0], 1)
        self.assertIsInstance(out[1], ValueError)
        self.assertEqual(out[2], 3)

    def test_single_worker_behaves_like_parallel(self):
        def fn(x):
            if x == "bad":
                raise ValueError("boom")
            return x
        items = ["ok", "bad", "fine"]
        self.assertEqual(str(verify.map_concurrent(fn, items, 1)[1]),
                         str(verify.map_concurrent(fn, items, 4)[1]))

    def test_empty(self):
        self.assertEqual(verify.map_concurrent(lambda x: x, [], 4), [])

    def test_normalize_concurrency_clamps(self):
        self.assertEqual(verify.normalize_concurrency(None), verify.SCAN_CONCURRENCY)
        self.assertEqual(verify.normalize_concurrency("abc"), verify.SCAN_CONCURRENCY)
        self.assertEqual(verify.normalize_concurrency(0), 1)
        self.assertEqual(verify.normalize_concurrency(-5), 1)
        self.assertEqual(verify.normalize_concurrency(9999), verify.SCAN_CONCURRENCY_MAX)
        self.assertEqual(verify.normalize_concurrency(4), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
