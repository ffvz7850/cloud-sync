# -*- coding: utf-8 -*-
"""upload_queue 状态机测试：成功、跳过、自动重试、手动重传、取消、重启恢复。

用 FakeClient 替掉真实网络，直接驱动队列的内部方法（不真起 worker 线程，
这样时序可控）。
"""
import os
import tempfile
import threading
import time
import unittest

import support  # noqa: F401

import openlist
import upload_queue


class FakeClient(object):
    """模拟 OpenList 客户端。fail_times 控制前几次 upload_resumable 抛错。"""

    def __init__(self):
        self.fail_times = 0
        self.uploads = []
        self.remote_files = {}   # remotePath -> size（stat 用）
        self.fail_with = None
        self.last_kwargs = {}    # 最后一次 upload_resumable 的关键字参数

    def upload_resumable(self, local, remote, on_progress=None, **kw):
        size = os.path.getsize(local)
        self.last_kwargs = dict(kw)
        if on_progress:
            on_progress(size // 2, size)
            on_progress(size, size)
        if self.fail_times > 0:
            self.fail_times -= 1
            exc = self.fail_with or openlist.NetworkError("模拟网络抖动")
            raise exc
        self.uploads.append((local, remote))
        self.remote_files[remote] = size
        return {"size": size, "path": remote}

    def stat(self, remote):
        if remote in self.remote_files:
            return {"name": os.path.basename(remote), "is_dir": False,
                    "size": self.remote_files[remote]}
        return None

    def ensure_dir(self, path):
        return None


def make_queue(client=None, max_attempts=3):
    support.temp_data_dir()
    path = os.path.join(os.environ["TRIM_PKGVAR"], "q.json")
    q = upload_queue.UploadQueue(client or FakeClient(), path=path,
                                 max_attempts=max_attempts, base_delay=0.01)
    return q


def make_file(content=b"hello world"):
    fd, path = tempfile.mkstemp()
    os.write(fd, content)
    os.close(fd)
    return path


def add_one(q, local, remote="/dst/a.txt"):
    tasks = q.add([{"localPath": local, "remotePath": remote}])
    assert len(tasks) == 1
    return tasks[0]


class AddTest(unittest.TestCase):
    def test_add_fields(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["size"], os.path.getsize(local))
        self.assertEqual(t["attempts"], 0)
        self.assertEqual(t["remotePath"], "/dst/a.txt")
        os.unlink(local)

    def test_add_skips_missing_file(self):
        q = make_queue()
        tasks = q.add([{"localPath": "/nonexistent-xyz", "remotePath": "/x"}])
        self.assertEqual(tasks, [])

    def test_add_accepts_precomputed_size(self):
        """调用方扫描时已经拿到大小就直接用，省掉一次 getsize。"""
        q = make_queue()
        local = make_file()
        tasks = q.add([{"localPath": local, "remotePath": "/dst/a.txt", "size": 9999}])
        self.assertEqual(tasks[0]["size"], 9999)
        os.unlink(local)

    def test_add_tolerates_bad_size_value(self):
        q = make_queue()
        local = make_file()
        tasks = q.add([{"localPath": local, "remotePath": "/dst/a.txt", "size": "abc"}])
        self.assertEqual(tasks[0]["size"], 0)
        os.unlink(local)


class RunSuccessTest(unittest.TestCase):
    def test_success_marks_done(self):
        client = FakeClient()
        q = make_queue(client)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        final = q.get(t["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["uploaded"], final["size"])
        self.assertEqual(client.uploads[0][1], "/dst/a.txt")
        os.unlink(local)

    def test_missing_local_marks_failed(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        os.unlink(local)                     # 上传前删掉本地文件
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "failed")

    def test_skip_existing(self):
        client = FakeClient()
        q = make_queue(client)
        local = make_file(b"samedata")
        client.remote_files["/dst/a.txt"] = os.path.getsize(local)   # 云端已有同大小
        t = add_one(q, local)
        # 打开 skipExisting
        import config
        cfg = config.load_config()
        cfg["skipExisting"] = True
        config.save_config(cfg)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "skipped")
        self.assertEqual(client.uploads, [])   # 没有真的上传
        os.unlink(local)


class FailEnsureDirClient(FakeClient):
    """ensure_dir 一律失败（云端只读 / 权限不足）。"""

    def ensure_dir(self, path):
        raise openlist.OpenlistError("目标只读")


class ClaimReleaseTest(unittest.TestCase):
    """`_claim` 之后的**每一条**退出路径都必须走到 `_release`。

    历史上三处提前 return 都漏了释放：skip-existing 命中、`ensure_dir` 失败、
    `getmtime` 读不到。后果不是报错，而是更阴的一种 —— `_running` 里留下 id，
    `_next_pending()` 从此永远跳过它，任务明明是 pending 却再也不会被执行，
    界面一直卡在「排队中」。所以这里直接盯 `_running`，而不是盯返回值。
    """

    def _assert_released(self, q, tid, label):
        self.assertNotIn(tid, q._running,
                         "%s：任务已走完这一轮，_running 里不该还留着它的 id" % label)
        # 更实在的后果：id 残留会让 worker 再也挑不到这个任务。
        # 把状态临时摆回 pending，看看挑选能不能命中。
        with q._lock:
            t = q._find(tid)
        self.assertIsNotNone(t, "%s：任务丢了" % label)
        saved = t["status"]
        t["status"] = upload_queue.PENDING
        try:
            self.assertIsNotNone(q._next_pending(),
                                 "%s：释放不干净 → worker 再也挑不到这个任务" % label)
        finally:
            t["status"] = saved

    def test_skip_existing_hit_releases_claim(self):
        client = FakeClient()
        q = make_queue(client)
        local = make_file(b"samedata")
        client.remote_files["/dst/a.txt"] = os.path.getsize(local)
        t = add_one(q, local)
        import config
        cfg = config.load_config()
        cfg["skipExisting"] = True
        config.save_config(cfg)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "skipped")
        self._assert_released(q, t["id"], "跳过已存在")
        os.unlink(local)

    def test_ensure_dir_failure_releases_claim(self):
        q = make_queue(FailEnsureDirClient())
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "failed")
        self.assertIn("云端目录", q.get(t["id"])["error"])
        self._assert_released(q, t["id"], "创建云端目录失败")
        os.unlink(local)

    def test_unreadable_mtime_releases_claim(self):
        """本地文件在"`stat` 之后、读 mtime 之前"被删：`getmtime` 抛 OSError，
        这条提前 return 也必须释放认领。"""
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        real_getmtime = os.path.getmtime
        os.path.getmtime = lambda p: (_ for _ in ()).throw(OSError("文件已消失"))
        try:
            q._run_task(t)
        finally:
            os.path.getmtime = real_getmtime
        self.assertEqual(q.get(t["id"])["status"], "failed")
        self.assertIn("不可读", q.get(t["id"])["error"])
        self._assert_released(q, t["id"], "本地文件不可读")
        if os.path.exists(local):
            os.unlink(local)


class RetryTest(unittest.TestCase):
    def test_auto_retry_then_success(self):
        client = FakeClient()
        client.fail_times = 1                 # 第一次失败，第二次成功
        q = make_queue(client, max_attempts=3)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)                        # 失败 → 重新入队
        after1 = q.get(t["id"])
        self.assertEqual(after1["status"], "pending")
        self.assertEqual(after1["attempts"], 1)
        q._run_task(q.get(t["id"]))           # 第二次成功
        self.assertEqual(q.get(t["id"])["status"], "done")
        os.unlink(local)

    def test_exhausted_retries_mark_failed(self):
        client = FakeClient()
        client.fail_times = 99
        q = make_queue(client, max_attempts=2)
        local = make_file()
        t = add_one(q, local)
        for _ in range(5):
            cur = q.get(t["id"])
            if cur["status"] != "pending":
                break
            q._run_task(cur)
        final = q.get(t["id"])
        self.assertEqual(final["status"], "failed")
        self.assertGreaterEqual(final["attempts"], 2)
        os.unlink(local)

    def test_auth_error_not_retried(self):
        client = FakeClient()
        client.fail_times = 99
        client.fail_with = openlist.AuthError("登录态失效")
        q = make_queue(client, max_attempts=5)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        final = q.get(t["id"])
        # 登录问题重试无意义，直接 failed，提示重新登录
        self.assertEqual(final["status"], "failed")
        self.assertIn("重新登录", final["error"])
        self.assertEqual(final["attempts"], 0)
        os.unlink(local)


class ManualOpsTest(unittest.TestCase):
    def test_manual_retry_resets(self):
        client = FakeClient()
        client.fail_times = 99
        q = make_queue(client, max_attempts=1)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "failed")
        client.fail_times = 0                 # 网络恢复了
        q.retry(t["id"])
        reset = q.get(t["id"])
        self.assertEqual(reset["status"], "pending")
        self.assertEqual(reset["attempts"], 0)
        q._run_task(reset)
        self.assertEqual(q.get(t["id"])["status"], "done")
        os.unlink(local)

    def test_cancel_pending(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        q.cancel(t["id"])
        self.assertEqual(q.get(t["id"])["status"], "cancelled")
        os.unlink(local)

    def test_clear_done(self):
        client = FakeClient()
        q = make_queue(client)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        removed = q.clear_done()
        self.assertEqual(removed, 1)
        self.assertEqual(q.list(), [])
        os.unlink(local)


class ResumeTest(unittest.TestCase):
    def test_interrupted_task_resets_to_pending(self):
        """应用重启时，进行中（uploading/verifying）的任务要重置回 pending。"""
        support.temp_data_dir()
        path = os.path.join(os.environ["TRIM_PKGVAR"], "q.json")
        local = make_file()
        q1 = upload_queue.UploadQueue(FakeClient(), path=path)
        tasks = q1.add([{"localPath": local, "remotePath": "/dst/a.txt"}])
        # 手动把它标记成 uploading（模拟上次运行中被中断）
        q1._set(tasks[0], status="uploading", uploaded=5)
        q1.save(force=True)

        # 重新加载（模拟重启）
        q2 = upload_queue.UploadQueue(FakeClient(), path=path)
        restored = q2.get(tasks[0]["id"])
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(restored["uploaded"], 0)
        os.unlink(local)


class CountsTest(unittest.TestCase):
    def test_counts(self):
        q = make_queue()
        l1 = make_file()
        l2 = make_file()
        q.add([{"localPath": l1, "remotePath": "/a"},
               {"localPath": l2, "remotePath": "/b"}])
        c = q.counts()
        self.assertEqual(c["total"], 2)
        self.assertEqual(c["pending"], 2)
        os.unlink(l1)
        os.unlink(l2)


class RetryAllTest(unittest.TestCase):
    """v2.1.0 新增：一键重试所有失败任务（retry_all）。"""

    def _seed(self, q):
        """放 5 个任务，覆盖 failed / done / pending / cancelled / uploading。"""
        locals_ = [make_file(b"x" * (i + 1)) for i in range(5)]
        tasks = q.add([{"localPath": lp, "remotePath": "/d/%d.bin" % i}
                       for i, lp in enumerate(locals_)])
        q._set(tasks[0], status="failed", attempts=3, error="网络错误", uploaded=5)
        q._set(tasks[1], status="done", uploaded=os.path.getsize(locals_[1]))
        q._set(tasks[2], status="pending")
        q._set(tasks[3], status="cancelled")
        q._set(tasks[4], status="uploading", uploaded=7)
        q.save(force=True)
        return locals_, tasks

    def test_retry_all_only_resets_failed(self):
        client = FakeClient()
        q = make_queue(client, max_attempts=3)
        locals_, tasks = self._seed(q)

        n = q.retry_all()
        self.assertEqual(n, 1, "只应重试 1 个 failed 任务")

        f = q.get(tasks[0]["id"])
        self.assertEqual(f["status"], "pending")
        self.assertEqual(f["attempts"], 0, "attempts 必须归零")
        self.assertEqual(f["error"], "", "error 必须清空")
        self.assertEqual(f["uploaded"], 5, "uploaded 要保留以配合 multipart 续传")

        # 非 failed 的任务一律不许被改动
        self.assertEqual(q.get(tasks[1]["id"])["status"], "done")
        self.assertEqual(q.get(tasks[2]["id"])["status"], "pending")
        self.assertEqual(q.get(tasks[3]["id"])["status"], "cancelled")
        self.assertEqual(q.get(tasks[4]["id"])["status"], "uploading")

        for lp in locals_:
            os.unlink(lp)

    def test_retry_all_persists(self):
        """状态变更必须立即落盘（重启后仍是 pending）。"""
        client = FakeClient()
        q = make_queue(client)
        locals_, tasks = self._seed(q)
        q.retry_all()

        q2 = upload_queue.UploadQueue(FakeClient(), path=q.path)
        self.assertEqual(q2.get(tasks[0]["id"])["status"], "pending")
        self.assertEqual(q2.get(tasks[0]["id"])["attempts"], 0)
        self.assertEqual(q2.get(tasks[0]["id"])["error"], "")

        for lp in locals_:
            os.unlink(lp)

    def test_retry_all_none_failed_returns_zero(self):
        q = make_queue()
        local = make_file()
        add_one(q, local)
        self.assertEqual(q.retry_all(), 0)
        self.assertEqual(q.retry_all(), 0)
        os.unlink(local)

    def test_retry_all_then_runs(self):
        """一键重试后任务能真正跑起来（网络恢复 → done）。"""
        client = FakeClient()
        client.fail_times = 99
        q = make_queue(client, max_attempts=1)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)                        # 耗尽重试 → failed
        self.assertEqual(q.get(t["id"])["status"], "failed")

        client.fail_times = 0                 # 网络恢复
        self.assertEqual(q.retry_all(), 1)
        q._run_task(q.get(t["id"]))
        self.assertEqual(q.get(t["id"])["status"], "done")
        os.unlink(local)


class RetryAllSideEffectTest(unittest.TestCase):
    """v2.1.0：retry_all 的**精确作用面** —— 独立构造混合状态队列，
    逐一比对非 failed 任务在重试前后是否**逐字段完全一致**（不只是状态没变）。"""

    KINDS = [
        ("done", {"uploaded": 999, "attempts": 1}),
        ("failed", {"uploaded": 42, "attempts": 3, "error": "网络错误"}),
        ("pending", {"uploaded": 0, "attempts": 0}),
        ("uploading", {"uploaded": 7, "attempts": 2}),
        ("cancelled", {"uploaded": 11, "attempts": 0}),
        ("skipped", {"uploaded": 5, "attempts": 0}),
    ]

    def _seed_mixed(self, q):
        locals_ = [make_file(b"z" * (i + 1)) for i in range(len(self.KINDS))]
        tasks = q.add([{"localPath": lp, "remotePath": "/d/%d" % i}
                       for i, lp in enumerate(locals_)])
        for task, (status, extra) in zip(tasks, self.KINDS):
            q._set(task, status=status, **extra)
        q.save(force=True)
        return locals_, tasks

    def test_retry_all_only_failed_changes(self):
        q = make_queue(FakeClient())
        locals_, tasks = self._seed_mixed(q)
        before = {t["id"]: dict(q.get(t["id"])) for t in tasks}

        n = q.retry_all()
        self.assertEqual(n, 1, "混合队列里只有 1 个 failed，只应重试它")

        for task, (status, _extra) in zip(tasks, self.KINDS):
            after = q.get(task["id"])
            if status == "failed":
                self.assertEqual(after["status"], "pending")
                self.assertEqual(after["attempts"], 0)
                self.assertEqual(after["error"], "")
                self.assertEqual(after["speed"], 0)
                self.assertEqual(after["finishedAt"], 0)
                self.assertEqual(after["uploaded"], 42, "failed 的 uploaded 必须保留")
                continue
            # 非 failed：整个 task 字典（含 id/时间戳/进度/尝试数）都不许变
            self.assertEqual(after, before[task["id"]],
                             "%s 任务被 retry_all 意外改动" % status)

        for lp in locals_:
            os.unlink(lp)

    def test_retry_all_uploaded_survives_reload(self):
        """uploaded 要能落盘并读回 —— 这是 multipart 断点续传的前提。"""
        q = make_queue(FakeClient())
        locals_, tasks = self._seed_mixed(q)
        q.retry_all()

        q2 = upload_queue.UploadQueue(FakeClient(), path=q.path)
        failed = [t for t, (st, _e) in zip(tasks, self.KINDS) if st == "failed"][0]
        restored = q2.get(failed["id"])
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(restored["attempts"], 0)
        self.assertEqual(restored["error"], "")
        self.assertEqual(restored["uploaded"], 42, "重启后 uploaded 丢失，续传会从头传")

        for lp in locals_:
            os.unlink(lp)

    def test_retry_all_clears_cancel_flag(self):
        q = make_queue(FakeClient())
        local = make_file()
        t = add_one(q, local)
        q._set(t, status="failed", attempts=1, error="e")
        import threading
        flag = threading.Event()
        flag.set()
        q._cancel_flags[t["id"]] = flag
        q.retry_all()
        self.assertNotIn(t["id"], q._cancel_flags,
                         "retry_all 必须清掉残留的取消旗标，否则重试任务会被立刻判取消")
        os.unlink(local)


class WorkerLifecycleTest(unittest.TestCase):
    """v2.1.0：用 `_wake` Event 取代 `time.sleep` 轮询后，worker 仍能
    被唤醒处理任务、且 `stop()` 能干净退出（线程不再存活、不卡死）。"""

    def _wait_status(self, q, tid, want, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = q.get(tid)
            if task and task.get("status") == want:
                return True
            time.sleep(0.02)
        return False

    def test_worker_processes_task_via_wake(self):
        client = FakeClient()
        q = make_queue(client)
        q.start(workers=1)
        try:
            local = make_file(b"payload")
            t = add_one(q, local)
            self.assertTrue(self._wait_status(q, t["id"], "done", timeout=3.0),
                            "worker 没在超时内完成上传（_wake 唤醒失效？）")
            self.assertTrue(client.uploads, "worker 根本没调用上传")
            os.unlink(local)
        finally:
            q.stop()

    def test_stop_when_idle_is_quick_and_joins(self):
        q = make_queue(FakeClient())
        q.start(workers=3)
        threads = list(q._workers)
        self.assertEqual(len(threads), 3)
        start = time.time()
        q.stop()
        elapsed = time.time() - start
        self.assertLess(elapsed, 2.0,
                        "空闲 worker 的 stop() 应被 _wake 立即唤醒，实际 %.2fs" % elapsed)
        self.assertEqual(q._workers, [])
        self.assertFalse(q.is_running())
        for th in threads:
            self.assertFalse(th.is_alive(), "stop() 后 worker 线程仍存活（卡死）")

    def test_stop_after_task_completes(self):
        client = FakeClient()
        q = make_queue(client)
        q.start(workers=2)
        local = make_file(b"x")
        t = add_one(q, local)
        self.assertTrue(self._wait_status(q, t["id"], "done", 3.0))
        threads = list(q._workers)
        start = time.time()
        q.stop()
        self.assertLess(time.time() - start, 2.0)
        for th in threads:
            self.assertFalse(th.is_alive())
        os.unlink(local)

    def test_retry_all_wakes_idle_worker(self):
        client = FakeClient()
        q = make_queue(client)
        local = make_file(b"retry-me")
        t = add_one(q, local)
        q._set(t, status="failed", attempts=9, error="boom")   # 先做成 failed
        q.save(force=True)
        q.start(workers=1)
        try:
            self.assertEqual(q.retry_all(), 1)
            self.assertTrue(self._wait_status(q, t["id"], "done", 3.0),
                            "retry_all 后 worker 没被唤醒处理")
        finally:
            q.stop()
        os.unlink(local)


class VerifyConfigTest(unittest.TestCase):
    """verifyAfterUpload 曾经是完全没用的死配置（永远按 True 走）。"""

    def _run(self, verify):
        import config
        client = FakeClient()
        q = make_queue(client)      # 先建队列：它会切到一个全新的隔离数据目录
        cfg = config.load_config()
        cfg["verifyAfterUpload"] = verify
        cfg["skipExisting"] = False
        config.save_config(cfg)
        local = make_file()
        t = add_one(q, local)
        q._run_task(t)
        os.unlink(local)
        return client, q.get(t["id"])

    def test_enabled_by_default_passes_verify_true(self):
        client, final = self._run(True)
        self.assertEqual(final["status"], "done")
        self.assertTrue(client.last_kwargs.get("verify"))

    def test_disabled_passes_verify_false(self):
        client, final = self._run(False)
        self.assertEqual(final["status"], "done")
        self.assertFalse(client.last_kwargs.get("verify"),
                         "关掉校验后不该再发回读请求")


class SkipExistingScopeTest(unittest.TestCase):
    """add(skip_existing=...) 曾是被完全忽略的参数。"""

    def _setup(self, global_skip, batch_skip):
        import config
        client = FakeClient()
        q = make_queue(client)      # 先建队列：它会切到一个全新的隔离数据目录
        cfg = config.load_config()
        cfg["skipExisting"] = global_skip
        config.save_config(cfg)
        local = make_file(b"samedata")
        client.remote_files["/dst/a.txt"] = os.path.getsize(local)
        tasks = q.add([{"localPath": local, "remotePath": "/dst/a.txt"}],
                      skip_existing=batch_skip)
        return client, q, tasks[0], local

    def test_batch_true_overrides_global_false(self):
        client, q, t, local = self._setup(False, True)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "skipped")
        self.assertEqual(client.uploads, [])
        os.unlink(local)

    def test_batch_false_overrides_global_true(self):
        client, q, t, local = self._setup(True, False)
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "done",
                         "任务级明确要求不跳过时必须真传")
        os.unlink(local)

    def test_none_follows_global(self):
        client, q, t, local = self._setup(True, None)
        self.assertIsNone(t["skipExisting"])
        q._run_task(t)
        self.assertEqual(q.get(t["id"])["status"], "skipped")
        os.unlink(local)


class ClaimTest(unittest.TestCase):
    """任务认领必须是原子的，否则同一文件会被并发重传。"""

    def test_double_claim_rejected(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        self.assertTrue(q._claim(t["id"]))
        self.assertFalse(q._claim(t["id"]), "已被占用的任务不能再次认领")
        self.assertIsNone(q._next_pending())
        q._release(t["id"])
        # 释放时状态已是 uploading，不再是 pending
        self.assertFalse(q._claim(t["id"]))
        os.unlink(local)

    def test_claim_rejects_cancelled_task(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        q.cancel(t["id"])                 # pending → cancelled
        self.assertFalse(q._claim(t["id"]), "已取消的任务不能被认领执行")
        os.unlink(local)

    def test_release_frees_cancel_flag(self):
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        q._claim(t["id"])
        q._cancel_flags[t["id"]].set()
        q._release(t["id"])
        self.assertFalse(q._is_cancelled(t["id"]))
        os.unlink(local)


class BackoffTest(unittest.TestCase):
    """退避期间任务不能被其它 worker 抢走。"""

    def test_no_concurrent_claim_during_backoff(self):
        client = FakeClient()
        q = make_queue(client, max_attempts=3)
        q.base_delay = 0.8
        local = make_file()
        t = add_one(q, local)
        self.assertTrue(q._claim(t["id"]))     # 模拟 worker A 已取走

        stolen = []
        stop = threading.Event()

        def rival():
            # 模拟 worker B 在退避窗口内不停找活干
            while not stop.is_set():
                if q._next_pending() is not None:
                    stolen.append(time.time())
                time.sleep(0.02)

        th = threading.Thread(target=rival)
        th.start()
        try:
            q._handle_failure(q.get(t["id"]), openlist.NetworkError("抖动"))
        finally:
            stop.set()
            th.join()

        self.assertEqual(stolen, [], "退避窗口内任务被别的 worker 抢走了")
        cur = q.get(t["id"])
        self.assertEqual(cur["status"], "pending")
        self.assertEqual(cur["attempts"], 1)
        self.assertEqual(cur["retryAt"], 0)
        os.unlink(local)

    def test_stop_during_backoff_leaves_recoverable_state(self):
        client = FakeClient()
        q = make_queue(client, max_attempts=3)
        q.base_delay = 2.0
        local = make_file()
        t = add_one(q, local)
        q._claim(t["id"])

        def stopper():
            time.sleep(0.1)
            q._stop.set()

        th = threading.Thread(target=stopper)
        th.start()
        start = time.time()
        q._handle_failure(q.get(t["id"]), openlist.NetworkError("抖动"))
        th.join()
        self.assertLess(time.time() - start, 1.5, "停止时退避应立即被打断")
        # 停在 uploading 落盘，等下次 load() 重置为 pending（可恢复，不是丢任务）
        self.assertEqual(q.get(t["id"])["status"], "uploading")
        os.unlink(local)


class CancelDuringRetryTest(unittest.TestCase):
    def test_cancel_wins_over_retry(self):
        """上传中点了取消，恰好这次失败 —— 不能又自己重试跑起来。"""
        client = FakeClient()
        client.fail_times = 99
        q = make_queue(client, max_attempts=3)
        local = make_file()
        t = add_one(q, local)
        q._claim(t["id"])
        q._cancel_flags[t["id"]].set()      # 用户取消
        q._handle_failure(q.get(t["id"]), openlist.NetworkError("抖动"))
        cur = q.get(t["id"])
        self.assertEqual(cur["status"], "cancelled")
        self.assertEqual(cur["attempts"], 1)
        self.assertEqual(cur["error"], "")
        os.unlink(local)

    def test_cancel_during_backoff_takes_effect(self):
        client = FakeClient()
        client.fail_times = 99
        q = make_queue(client, max_attempts=3)
        q.base_delay = 1.0
        local = make_file()
        t = add_one(q, local)
        q._claim(t["id"])

        def canceller():
            time.sleep(0.1)
            q.cancel(t["id"])

        th = threading.Thread(target=canceller)
        th.start()
        q._handle_failure(q.get(t["id"]), openlist.NetworkError("抖动"))
        th.join()
        cur = q.get(t["id"])
        self.assertEqual(cur["status"], "cancelled")
        self.assertEqual(cur["attempts"], 1, "取消的任务不该被重置尝试计数")
        os.unlink(local)


class SaveIsolationTest(unittest.TestCase):
    def test_payload_does_not_share_task_objects(self):
        """落盘快照必须与内存对象解耦，否则写入过程会被进度更新撞车。"""
        import config
        q = make_queue()
        local = make_file()
        t = add_one(q, local)
        captured = {}
        orig = config.atomic_write_json

        def spy(path, payload):
            captured["payload"] = payload
            return orig(path, payload)

        config.atomic_write_json = spy
        try:
            q.save(force=True)
        finally:
            config.atomic_write_json = orig
        snap = captured["payload"]["tasks"][0]
        self.assertIsNot(snap, t)
        snap["uploaded"] = 12345
        self.assertEqual(q.get(t["id"])["uploaded"], 0,
                         "改快照不该影响内存里的任务")
        os.unlink(local)


if __name__ == "__main__":
    unittest.main(verbosity=2)
