# -*- coding: utf-8 -*-
"""下载任务队列（云端 → 本地）。

直接继承 `UploadQueue`：持久化、重启后恢复、自动重试退避、暂停 / 取消 /
清除已结束，这些机制与上传**完全同构**，再抄一份只会多一份要同步维护的
代码。这里只覆写两处：

* `add()`      —— 入队时打上 `direction="download"`，并允许本地目标尚不存在；
* `_run_task()` —— 把「PUT 上传」换成「GET 流式下载到本地」。

任务结构沿用上传队列的字段名（`localPath` = 本地落点，`remotePath` = 云端
来源，`uploaded` = 已下载字节），前端因此可以走同一套列表渲染。
"""

import os
import threading
import time

import config
import openlist
import upload_queue
from upload_queue import (CANCELLED, DONE, FAILED, PENDING,  # noqa: F401
                          SKIPPED, UPLOADING, VERIFYING)     # noqa: F401

DIRECTION = "download"


class DownloadQueue(upload_queue.UploadQueue):
    def __init__(self, client, path=None, log=None, max_attempts=3, base_delay=2.0):
        upload_queue.UploadQueue.__init__(
            self, client,
            path=path or os.path.join(config.data_dir(), "download_queue.json"),
            log=log, max_attempts=max_attempts, base_delay=base_delay)

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------

    def add(self, items, overwrite=True):
        """`items`: `[{"remotePath", "localPath", "size"}]`。

        `size` 由调用方（比对结果）给出：下载任务的本地文件**还不存在**，
        基类的 `os.path.isfile` 兜底会把它当"源文件丢失"直接丢弃。给了 size
        就跳过那次检查；真的拿不到大小就传 0，运行时再从云端条目补齐。
        """
        clean = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            remote = (item.get("remotePath") or "").strip()
            local = (item.get("localPath") or "").strip()
            if not remote or not local:
                continue
            try:
                size = max(0, int(item.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            clean.append({"localPath": local, "remotePath": remote, "size": size})

        added = upload_queue.UploadQueue.add(self, clean, skip_existing=False)
        if not added:
            return added
        with self._lock:
            for task in added:
                task["direction"] = DIRECTION
                # 名字用云端文件名：本地路径可能因为"落点目录"而与源不同名
                task["name"] = task["remotePath"].rsplit("/", 1)[-1] or task["name"]
                task["overwrite"] = bool(overwrite)
        self.save(force=True)
        return added

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _run_task(self, task):
        tid = task["id"]
        remote = task["remotePath"]
        local = task["localPath"]

        if not self._claim(tid):
            return

        try:
            cancel_flag = self._cancel_flags.setdefault(tid, threading.Event())

            parent = os.path.dirname(local)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:
                    self._mark_failed(task, "创建本地目录失败：%s（%s）" % (parent, exc))
                    return

            # overwrite 语义：False 时本地已存在同名文件则跳过，不覆盖。
            # download_to_file 内部是 os.replace 无条件覆盖，所以这里先判断。
            if not task.get("overwrite", True) and os.path.isfile(local):
                self._set(task, status=SKIPPED,
                          uploaded=int(task.get("size") or 0),
                          finishedAt=int(time.time()))
                self.save(force=True)
                self._log("跳过已存在：%s" % local)
                return

            progress_state = {"last_t": time.time(), "last_sent": 0}

            def on_progress(got, total):
                now = time.time()
                with self._lock:
                    t = self._find(tid)
                    if not t:
                        return
                    dt = now - progress_state["last_t"]
                    if dt >= 0.5:
                        if progress_state["last_sent"] > 0:
                            t["speed"] = max(0, int((got - progress_state["last_sent"]) / dt))
                        progress_state["last_t"] = now
                        progress_state["last_sent"] = got
                    t["uploaded"] = got
                    if total and int(total) > 0:
                        t["size"] = int(total)
                self.save()      # 节流落盘

            try:
                result = self.client.download_to_file(
                    remote, local,
                    on_progress=on_progress,
                    should_cancel=lambda: cancel_flag.is_set() or self._stop.is_set(),
                    expected_size=int(task.get("size") or 0) or None)
            except openlist.UploadCancelled:
                self._set(task, status=CANCELLED, speed=0, cancelling=False,
                          finishedAt=int(time.time()))
                self.save(force=True)
                self._log("下载已取消：%s" % remote)
                return
            except openlist.AuthError as exc:
                self._mark_failed(task, "登录态失效：%s（请在「连接」里重新登录）" % exc)
                return
            except openlist.OpenlistError as exc:
                self._handle_failure(task, exc)
                return

            size = int(result.get("size") or 0) or int(task.get("size") or 0)
            self._set(task, status=DONE, uploaded=size, size=size,
                      finishedAt=int(time.time()), speed=0, cancelling=False)
            self.save(force=True)
            self._log("下载完成：%s → %s" % (remote, local))
        finally:
            # 认领/释放必须成对，否则任务永远卡在「有人正在跑」而再也取不到
            self._release(tid)
