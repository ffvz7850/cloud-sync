# -*- coding: utf-8 -*-
"""持久化上传任务队列 —— 「百度网盘客户端」体验的核心。

能力：
* 任务持久化到磁盘，**应用重启后未完成的任务自动恢复**（multipart 断点续传，
  已传的分片不会重传）。
* 每个任务有进度（已传字节 / 总字节）、速度估算、状态机。
* 失败**自动重试**（指数退避，默认 3 次）；耗尽后转 failed，可**手动重传**。
* 上传完成后由 openlist 层做**完整性校验**（比对云端大小）。
* 可选「跳过已存在」：云端已有同大小文件则标记 skipped，不重复传。

线程模型：N 个 worker 线程共享一个任务列表 + 一把锁，各自取 pending 任务执行。
持久化做了节流 —— 进度每 ~1.5s 落一次盘，状态变更立即落盘，避免每发一块
就写一次文件。
"""

import os
import threading
import time
import uuid

import config
import openlist

# 任务状态
PENDING = "pending"
UPLOADING = "uploading"
VERIFYING = "verifying"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"

TERMINAL = (DONE, FAILED, SKIPPED, CANCELLED)   # 终态
ACTIVE = (PENDING, UPLOADING, VERIFYING)

SAVE_INTERVAL = 1.5     # 进度落盘节流（秒）


class UploadQueue(object):
    def __init__(self, client, path=None, log=None, max_attempts=3, base_delay=2.0):
        self.client = client            # OpenlistClient（token 续期它内部处理）
        self.path = path or os.path.join(config.data_dir(), "upload_queue.json")
        self._log = log or (lambda *a, **k: None)
        self.max_attempts = max_attempts
        self.base_delay = base_delay

        # 用可重入锁：on_progress/save 等路径会在持锁状态下再次进入临界区，
        # 普通 Lock 在这种嵌套下会死锁（test_cancel_pending 就是踩在这上面）。
        self._lock = threading.RLock()
        self._tasks = []                # list[dict]
        self._load_failed = False       # 解析失败时置位，拒绝自动 save 空列表
        self._cancel_flags = {}         # task_id -> threading.Event（仅内存）
        # 正在被某个 worker 执行的任务 id。退避等待期间也保持占用，
        # 否则任务一旦被重置成 pending，其它 worker 会立刻抢走并发重传同一个文件。
        self._running = set()
        self._stop = threading.Event()
        self._wake = threading.Event()  # worker 空转时靠它被瞬间唤醒（不用干等轮询）
        self._paused = threading.Event()    # set() = 暂停：worker 不再取新任务
        self._workers = []
        self._last_save = 0.0
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self):
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            tasks = data.get("tasks") if isinstance(data, dict) else None
            if isinstance(tasks, list):
                self._tasks = [t for t in tasks if isinstance(t, dict)]
        except Exception as exc:
            # 解析失败不能静默清零：下一次 save() 会把空列表写回，原文件彻底消失。
            self._log("上传队列解析失败，已备份原文件：%s" % exc)
            try:
                import time
                bak = self.path + ".corrupt-%d" % int(time.time())
                if os.path.exists(self.path):
                    os.rename(self.path, bak)
                    self._log("坏文件已备份为：%s" % bak)
            except Exception:
                pass
            self._tasks = []
            self._load_failed = True
        # 恢复：上次运行时处在进行中的任务，重置回 pending 重跑。
        # multipart 会按 path+size 找回会话，已传分片不会重传，所以从头跑不亏。
        resumed = 0
        for task in self._tasks:
            if task.get("status") in (UPLOADING, VERIFYING):
                task["status"] = PENDING
                task["uploaded"] = 0
                task["error"] = ""
                task["retryAt"] = 0
                task["cancelling"] = False
                resumed += 1
        if resumed:
            self._log("恢复 %d 个未完成的上传任务" % resumed)

    def save(self, force=False):
        # 加载失败且当前没有任务 → 拒绝自动 save 空列表，避免覆盖原文件。
        if self._load_failed and not self._tasks:
            return
        now = time.time()
        with self._lock:
            if not force and (now - self._last_save) < SAVE_INTERVAL:
                return
            self._last_save = now
            # 在锁内就地把任务浅拷贝出来：json.dump 是在锁外跑的，
            # 直接引用 self._tasks 会让落盘过程与 on_progress 的写入撞车
            # （字典在迭代中被改 → 半截文件或 RuntimeError）。
            payload = {"version": 2, "savedAt": int(now),
                       "tasks": [dict(t) for t in self._tasks]}
        try:
            config.atomic_write_json(self.path, payload)
            self._load_failed = False    # 成功保存后清除失败标志
        except Exception as exc:
            self._log("上传队列落盘失败：%s" % exc)

    # ------------------------------------------------------------------
    # 任务 CRUD
    # ------------------------------------------------------------------

    def add(self, items, skip_existing=None):
        """批量加入任务。items: [{"localPath","remotePath"[,"size"]}]，返回新增的任务。

        skip_existing: True/False 显式指定本批任务是否「云端已有同大小文件就跳过」；
                       None（默认）表示跟随全局配置 skipExisting，且运行时以最新配置为准。
        逐任务记录，避免全局开关中途被改就把已入队任务的语义悄悄改掉。

        `size` 可选：调用方如果扫描时已经拿到大小（`expand_local_dir` 就是），
        直接带进来即可省掉这里的一次 `getsize`。不传则按老路径现查，
        并且**顺带用 isfile 过滤掉已消失的文件**。
        """
        added = []
        dropped = []
        for item in items or []:
            local = (item.get("localPath") or "").strip()
            remote = (item.get("remotePath") or "").strip()
            if not local or not remote:
                dropped.append((item.get("localPath") or "", "缺少本地或云端路径"))
                continue
            size = item.get("size")
            if size is None:
                if not os.path.isfile(local):
                    dropped.append((local, "本地文件不存在"))
                    continue
                try:
                    size = os.path.getsize(local)
                except OSError:
                    size = 0
            else:
                try:
                    size = max(0, int(size))
                except (TypeError, ValueError):
                    size = 0
            task = {
                "id": uuid.uuid4().hex[:12],
                "name": os.path.basename(local),
                "localPath": local,
                "remotePath": remote,
                "size": size,
                "uploaded": 0,
                "status": PENDING,
                "attempts": 0,
                "maxAttempts": self.max_attempts,
                "error": "",
                "speed": 0,
                "createdAt": int(time.time()),
                "startedAt": 0,
                "finishedAt": 0,
                "retryAt": 0,
                # None = 跟随全局 skipExisting；True/False = 本任务固定
                "skipExisting": None if skip_existing is None else bool(skip_existing),
            }
            with self._lock:
                self._tasks.append(task)
            added.append(task)
        if added:
            self.save(force=True)
            self._wake_workers()
            self._log("新增 %d 个上传任务" % len(added))
        # 被丢弃的条目要留下痕迹：前端只报"新增 0 个"时用户无从判断原因
        for path, why in dropped[:20]:
            self._log("跳过入队（%s）：%s" % (why, path))
        if len(dropped) > 20:
            self._log("另有 %d 个条目被跳过入队" % (len(dropped) - 20))
        return added

    def list(self):
        with self._lock:
            return [dict(t) for t in self._tasks]

    def get(self, task_id):
        with self._lock:
            for t in self._tasks:
                if t.get("id") == task_id:
                    return dict(t)
        return None

    def retry(self, task_id):
        """手动重传：重置为 pending、清零进度与尝试计数。"""
        with self._lock:
            t = self._find(task_id)
            if not t:
                return None
            t["status"] = PENDING
            t["uploaded"] = 0
            t["attempts"] = 0
            t["error"] = ""
            t["speed"] = 0
            t["finishedAt"] = 0
            t["retryAt"] = 0
            t["cancelling"] = False
        self._cancel_flags.pop(task_id, None)
        self.save(force=True)
        self._wake_workers()
        return self.get(task_id)

    def retry_all(self):
        """一键重试：把所有 failed 任务重置为 pending，返回重试的任务条数。

        与单条 `retry()` 的差别：这里**保留 uploaded**，配合 OpenList 的
        multipart 续传（按 path+size 找回未完成会话）从断点继续，而不是从头再传。
        只动 failed —— done / pending / cancelled / uploading 一律不碰。
        状态变更立即落盘，并唤醒空转的 worker。
        """
        retried = 0
        with self._lock:
            for t in self._tasks:
                if t.get("status") != FAILED:
                    continue
                t["status"] = PENDING
                t["attempts"] = 0
                t["error"] = ""
                t["speed"] = 0
                t["finishedAt"] = 0
                t["retryAt"] = 0
                t["cancelling"] = False
                # 注意：不动 t["uploaded"]，留给 multipart 续传用
                self._cancel_flags.pop(t.get("id"), None)
                retried += 1
        if retried:
            self.save(force=True)
            self._wake_workers()
            self._log("一键重试 %d 个失败任务" % retried)
        return retried

    def cancel(self, task_id):
        """取消：pending 直接转 cancelled；进行中设置取消旗标让上传循环停下来。"""
        need_save = False
        with self._lock:
            t = self._find(task_id)
            if not t:
                return None
            if t["status"] == PENDING:
                t["status"] = CANCELLED
                t["finishedAt"] = int(time.time())
                need_save = True
                result = dict(t)
            elif t["status"] in (UPLOADING, VERIFYING):
                flag = self._cancel_flags.setdefault(task_id, threading.Event())
                flag.set()
                # 上传循环会在下一个分片边界退出；期间界面显示「取消中」，
                # 而不是让用户以为点了没反应。
                t["cancelling"] = True
                need_save = True
                result = dict(t)
            else:
                result = dict(t)
        if need_save:
            self.save(force=True)
        return result

    def remove(self, task_id):
        with self._lock:
            t = self._find(task_id)
            if t and t["status"] in (UPLOADING, VERIFYING):
                flag = self._cancel_flags.setdefault(task_id, threading.Event())
                flag.set()
            self._tasks = [x for x in self._tasks if x.get("id") != task_id]
        self.save(force=True)
        return True

    def clear_done(self):
        """清除所有终态任务。"""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.get("status") not in TERMINAL]
            removed = before - len(self._tasks)
        if removed:
            self.save(force=True)
        return removed

    def counts(self):
        with self._lock:
            out = {"total": len(self._tasks)}
            for t in self._tasks:
                out[t.get("status", "?")] = out.get(t.get("status", "?"), 0) + 1
        return out

    def _find(self, task_id):
        for t in self._tasks:
            if t.get("id") == task_id:
                return t
        return None

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------

    def start(self, workers=2):
        if self._workers:
            return
        self._stop.clear()
        workers = max(1, int(workers or 2))
        for i in range(workers):
            th = threading.Thread(target=self._worker_loop,
                                  name="upload-worker-%d" % i, daemon=True)
            th.start()
            self._workers.append(th)
        self._log("上传队列已启动（%d 个并发）" % workers)

    def stop(self):
        self._stop.set()
        self._wake.set()        # 让空转的 worker 立刻醒来退出，不用干等轮询周期
        for th in self._workers:
            th.join(timeout=5)
        self._workers = []
        with self._lock:
            self._running.clear()   # worker 都退了，认领表必须清空
        self.save(force=True)

    def is_running(self):
        return bool(self._workers)

    def _wake_workers(self):
        """有新任务 / 重试任务时唤醒空转的 worker，缩短排队延迟。"""
        self._wake.set()

    def _next_pending(self):
        with self._lock:
            for t in self._tasks:
                if t.get("status") == PENDING and t.get("id") not in self._running:
                    return t
        return None

    def _claim(self, task_id):
        """原子认领：确认仍是 pending、未被取消，然后置为 uploading 并清空取消旗标。

        这几步必须挤在同一个锁里。拆开做的话，`cancel()` 恰好插在中间时
        （pending → cancelled 之后又被改成 uploading）用户的取消会被吞掉。
        """
        with self._lock:
            if task_id in self._running:
                return False
            t = self._find(task_id)
            if not t or t.get("status") != PENDING:
                return False
            flag = self._cancel_flags.setdefault(task_id, threading.Event())
            flag.clear()
            t["status"] = UPLOADING
            t["startedAt"] = int(time.time())
            t["error"] = ""
            t["speed"] = 0
            t["retryAt"] = 0
            t["cancelling"] = False
            self._running.add(task_id)
        self.save(force=True)
        return True

    def _release(self, task_id):
        with self._lock:
            self._running.discard(task_id)
            self._cancel_flags.pop(task_id, None)

    def _is_cancelled(self, task_id):
        flag = self._cancel_flags.get(task_id)
        return bool(flag is not None and flag.is_set())

    def _skip_existing_enabled(self, task):
        """任务级 skipExisting 优先，回退到全局配置。"""
        per_task = task.get("skipExisting")
        if per_task is not None:
            return bool(per_task)
        return bool(config.load_config().get("skipExisting"))

    def set_paused(self, paused):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
            self._wake_workers()
        self._log("上传队列已" + ("暂停" if paused else "恢复"))

    def is_paused(self):
        return self._paused.is_set()

    def cancel_all(self):
        n = 0
        with self._lock:
            for t in self._tasks:
                st = t.get("status")
                if st in (PENDING, UPLOADING, VERIFYING):
                    if st == PENDING:
                        t["status"] = CANCELLED
                        t["finishedAt"] = int(time.time())
                    else:
                        flag = self._cancel_flags.setdefault(t["id"], threading.Event())
                        flag.set()
                    n += 1
        if n:
            self.save(force=True)
            self._log("全部取消：%d 个任务" % n)
        return n

    def _worker_loop(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self._wake.wait(0.4)
                self._wake.clear()
                continue
            task = self._next_pending()
            if not task:
                # 用可被 set() 打断的等待替代死等 sleep：入队/重试能立即唤醒 worker。
                self._wake.wait(0.4)
                self._wake.clear()
                continue
            try:
                self._run_task(task)
            except Exception as exc:    # 兜底，绝不让 worker 线程死掉
                self._log("上传任务异常：%s" % exc)
                self._mark_failed(task, "内部错误：%s" % exc)
            # 注意：_run_task 内部 finally 已经做了 _release，这里不要再释放一次。
            # 双 release 会在「A 释放→B 认领→A 再释放」的时序下抹掉 B 的认领，
            # 导致 B、C 并发上传同一个文件。

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _set(self, task, **kv):
        with self._lock:
            t = self._find(task["id"])
            if t:
                t.update(kv)

    def _run_task(self, task):
        tid = task["id"]
        local = task["localPath"]
        remote = task["remotePath"]

        if not os.path.isfile(local):
            self._mark_failed(task, "本地文件不存在：%s" % local)
            return

        # 原子认领：确认任务仍是 pending 且没被取消，然后立刻置 uploading。
        # 放在这里（而不是更晚）是为了堵住「刚被取消又被改成 uploading」的窗口。
        if not self._claim(tid):
            return

        # claim 之后的所有逻辑必须在这个 try/finally 里，finally 统一 _release。
        # 之前 skip-existing 命中、ensure_dir 失败、mtime 读取失败三个提前 return
        # 都漏了 _release，导致 _running 残留 → 重试后任务永久卡在 PENDING。
        try:
            # 跳过已存在（可选）：云端同路径同大小 → skipped
            if self._skip_existing_enabled(task):
                try:
                    node = self.client.stat(remote)
                    if node and not node.get("is_dir") \
                            and int(node.get("size") or 0) == int(task["size"]):
                        self._set(task, status=SKIPPED, uploaded=task["size"],
                                  finishedAt=int(time.time()))
                        self.save(force=True)
                        self._log("跳过已存在：%s" % remote)
                        return
                except openlist.OpenlistError:
                    pass    # 查询失败不阻塞上传

            # 确保云端父目录存在
            parent = remote.rsplit("/", 1)[0] or "/"
            try:
                self.client.ensure_dir(parent)
            except openlist.OpenlistError as exc:
                self._mark_failed(task, "创建云端目录失败：%s" % exc)
                return

            cancel_flag = self._cancel_flags.setdefault(tid, threading.Event())
            progress_state = {"last_t": time.time(), "last_sent": 0}

            def on_progress(sent, total):
                now = time.time()
                with self._lock:
                    t = self._find(tid)
                    if not t:
                        return
                    dt = now - progress_state["last_t"]
                    if dt >= 0.5:
                        # 首次测速时以当前 sent 为基准，避免断点续传把
                        # 已传分片的累计字节当成瞬时速度算成几百 MB/s。
                        if progress_state["last_sent"] > 0:
                            t["speed"] = max(0, int((sent - progress_state["last_sent"]) / dt))
                        progress_state["last_t"] = now
                        progress_state["last_sent"] = sent
                    t["uploaded"] = sent
                self.save()     # 节流落盘

            cfg = config.load_config()
            # 「上传完成后读回云端比对大小」是用户可关的：关掉就不再发校验请求，
            # 代价是服务端静默少写字节也不会被发现。
            verify = bool(cfg.get("verifyAfterUpload", True))
            try:
                mtime = os.path.getmtime(local)
            except OSError:
                self._mark_failed(task, "本地文件不可读：%s" % local)
                return
            try:
                self.client.upload_resumable(
                    local, remote,
                    on_progress=on_progress,
                    chunk_size=config.setting_int("UPLOAD_CHUNK_MB", 10) * 1024 * 1024,
                    overwrite=True,
                    last_modified=mtime,
                    verify=verify,
                    should_cancel=lambda: cancel_flag.is_set() or self._stop.is_set(),
                )
                self._set(task, status=DONE, uploaded=task["size"],
                          finishedAt=int(time.time()), speed=0, cancelling=False)
                self.save(force=True)
                self._log("上传完成：%s → %s" % (local, remote))
            except openlist.UploadCancelled:
                self._set(task, status=CANCELLED, finishedAt=int(time.time()),
                          speed=0, cancelling=False)
                self.save(force=True)
                self._log("上传已取消：%s" % remote)
            except openlist.AuthError as exc:
                # 登录态失效且重登失败 —— 重试无意义，直接 failed 让用户去重新登录
                self._mark_failed(task, "登录态失效：%s（请在「连接」里重新登录）" % exc,
                                  retryable=False)
            except openlist.OpenlistError as exc:
                self._handle_failure(task, exc)
        finally:
            # 认领/释放必须成对。放在这里而不是只丢给 worker 循环，
            # 是因为 _run_task 也可能被直接调用（同步补跑、测试），
            # 漏掉释放会让任务永远卡在「有人正在跑」而再也取不到。
            self._release(tid)

    def _handle_failure(self, task, exc):
        """失败处理：可重试的进入退避，否则转 failed。

        退避期间**不能**把状态提前改成 pending —— 任务一旦是 pending，
        别的 worker 会立刻取走并发重传同一个文件（退避等于没做）。
        这里保持 uploading + retryAt，并继续占用 `_running`，等退避走完
        才放回 pending；期间用户取消、或队列停止都能立刻打断。
        """
        tid = task["id"]
        with self._lock:
            t = self._find(tid)
            if not t:
                return
            t["attempts"] = int(t.get("attempts") or 0) + 1
            attempts = t["attempts"]
            retryable = attempts < int(t.get("maxAttempts") or self.max_attempts)

        # 4xx 错误（路径冲突/权限/参数错误）重试无意义，直接失败。
        # 否则白占 worker + 误导用户（退避 6 秒后还是同样的错）。
        status = getattr(exc, "status", None) or getattr(exc, "code", None)
        if isinstance(status, int) and 400 <= status < 500:
            retryable = False

        # 取消优先于重试：用户点了取消就不该再自己跑起来。
        # （旧实现先把状态改成 pending 再检查旗标，旗标命中后只是提前结束等待，
        #   任务已经是 pending —— 取消被吞掉，文件照样会被重传。）
        if self._is_cancelled(tid):
            self._set(task, status=CANCELLED, finishedAt=int(time.time()),
                      speed=0, cancelling=False)
            self.save(force=True)
            self._log("上传已取消（失败重试前）：%s" % task.get("remotePath"))
            return

        if not retryable:
            self._mark_failed(task, str(exc))
            return

        delay = self.base_delay * (2 ** (attempts - 1))
        self._log("上传失败（第 %d 次），%.0fs 后重试：%s（%s）"
                  % (attempts, delay, task["remotePath"], exc))
        self._set(task, status=UPLOADING, speed=0,
                  retryAt=int(time.time() + delay), cancelling=False,
                  error="第 %d 次失败，%.0fs 后自动重试：%s" % (attempts, delay, exc))
        self.save(force=True)

        waited = 0.0
        while waited < delay:
            chunk = min(0.5, delay - waited)
            if self._stop.wait(chunk):
                # 队列停止：保持 uploading 落盘即可，下次 load() 会重置成 pending
                return
            waited += chunk
            if self._is_cancelled(tid):
                self._set(task, status=CANCELLED, finishedAt=int(time.time()),
                          speed=0, cancelling=False, retryAt=0)
                self.save(force=True)
                self._log("上传已取消（退避中）：%s" % task.get("remotePath"))
                return

        self._set(task, status=PENDING, uploaded=0, speed=0, retryAt=0)
        self.save(force=True)

    def _mark_failed(self, task, message, retryable=False):
        self._set(task, status=FAILED, error=message, speed=0,
                  finishedAt=int(time.time()))
        self.save(force=True)
        self._log("上传失败：%s → %s（%s）"
                  % (task.get("localPath"), task.get("remotePath"), message))
