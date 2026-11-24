# -*- coding: utf-8 -*-
"""传输记录：把「服务端同步完成 / 本地直接复制」的复制留痕。

## 为什么需要它（BUG-3）

有些复制**不产生任何队列任务**：

* **本地 → 本地**（`/api/sync/copy` 的 `side=local`）走 `shutil` 直接复制；
* **云端 → 云端同存储复制**——OpenList 是**同步**完成的，响应里根本没有 `tasks`
  （只有跨存储复制才会各自建一个后台任务）。

这两种情况以前既没有上传/下载队列任务、也没有 OpenList 任务，传输列表因此一片
空白：用户点了「复制」，只看到一句 toast，列表里什么都没有，以为根本没干活。
本模块就是给这两条链路补一份**回执**。

## 存储策略

* 内存里保留最近 `MAX_RECORDS` 条，落盘 `transfer_records.json`（原子写）；
* 记录天然都是**终态**（写完就是 done / failed），所以没有状态机、没有进度，
  也不需要「重试」——要重做就回对比页重新勾选，那才是唯一正确的入口；
* 有界 + 只保留回执，是因为它不是账本：传输列表只需要回答"刚刚干了什么"。

## id 约定

`lc_<hex>`（local copy）/ `olc_<hex>`（OpenList copy）。**刻意与上传队列的
`uuid4().hex[:12]`、OpenList 任务的 `ol_<tid>` 区分开** —— 三者混在一个列表里，
id 前缀是前端和后端判断"这条该走哪个接口"的唯一依据。
"""

import os
import threading
import time
import uuid

import config

# 最多保留多少条（再老的记录没有回执价值，只会撑大列表与落盘体积）
MAX_RECORDS = 500

KIND_LOCAL = "local"
KIND_CLOUD = "cloud"

# 方向标识与上传/下载队列的 direction 同一套词表（前端靠它选图标与文案）
DIRECTION_LOCAL_COPY = "localcopy"
DIRECTION_CLOUD_COPY = "cloudcopy"

DONE = "done"
FAILED = "failed"
TERMINAL = (DONE, FAILED)

# id 前缀：与「uuid 任务 id」「ol_ 前缀的 OpenList 任务 id」互不冲突
ID_PREFIX = {KIND_LOCAL: "lc_", KIND_CLOUD: "olc_"}

RECORD_FILE = "transfer_records.json"


def records_path():
    """记录文件路径（跟 config 的其它落盘文件同目录）。"""
    return os.path.join(config.data_dir(), RECORD_FILE)


def _as_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _name_of(item, src, dst):
    """记录显示名：优先调用方给的，其次目标名，最后源名。"""
    name = str(item.get("name") or "").strip()
    if name:
        return name
    for path in (dst, src):
        text = str(path or "").replace("\\", "/").rstrip("/")
        if text:
            tail = text.rsplit("/", 1)[-1]
            if tail:
                return tail
    return "(未命名)"


class TransferRecordStore(object):
    """有界 + 落盘的传输记录表（线程安全）。"""

    def __init__(self, path=None, limit=MAX_RECORDS, log=None):
        self.path = path or records_path()
        self.limit = max(1, int(limit or MAX_RECORDS))
        self._log = log or (lambda *a, **k: None)
        # RLock：save() 可能在持锁路径里被调用（add_many / remove 都是）
        self._lock = threading.RLock()
        self._records = []
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self):
        """读回上次的记录。坏文件不能静默清空后又被 save 覆盖掉。"""
        data = config.read_cached_json(self.path, {})
        records = data.get("records") if isinstance(data, dict) else None
        if isinstance(records, list):
            self._records = [r for r in records if isinstance(r, dict) and r.get("id")]
        else:
            self._records = []
        if len(self._records) > self.limit:
            self._records = self._records[-self.limit:]

    def save(self):
        with self._lock:
            payload = {"version": 1, "savedAt": int(time.time()),
                       "records": [dict(r) for r in self._records]}
        try:
            config.atomic_write_json(self.path, payload)
        except Exception as exc:
            self._log("传输记录落盘失败：%s" % exc)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def _build(self, item):
        item = item if isinstance(item, dict) else {}
        kind = str(item.get("kind") or "").strip().lower()
        kind = KIND_CLOUD if kind == KIND_CLOUD else KIND_LOCAL
        status = str(item.get("status") or "").strip().lower()
        status = FAILED if status == FAILED else DONE
        src = str(item.get("srcPath") or "")
        dst = str(item.get("dstPath") or "")
        now = int(time.time())
        return {
            "id": ID_PREFIX[kind] + uuid.uuid4().hex[:12],
            "name": _name_of(item, src, dst),
            "srcPath": src,
            "dstPath": dst,
            "size": _as_int(item.get("size")),
            "isDir": bool(item.get("isDir")),
            "status": status,
            "error": str(item.get("error") or ""),
            "kind": kind,
            "direction": (DIRECTION_CLOUD_COPY if kind == KIND_CLOUD
                          else DIRECTION_LOCAL_COPY),
            # 前端识别「这不是队列任务」的唯一标记：不画进度条、不给重试，
            # 只给「删除」。
            "isRecord": True,
            "createdAt": now,
            "finishedAt": now,
        }

    def add_many(self, entries):
        """批量写入记录，返回新建的记录列表。"""
        built = [self._build(item) for item in (entries or [])]
        if not built:
            return []
        with self._lock:
            self._records.extend(built)
            if len(self._records) > self.limit:
                self._records = self._records[-self.limit:]
        self.save()
        return built

    def add(self, item):
        built = self.add_many([item])
        return built[0] if built else None

    # ------------------------------------------------------------------
    # 读取 / 删除
    # ------------------------------------------------------------------

    def list(self):
        with self._lock:
            return [dict(r) for r in self._records]

    def get(self, record_id):
        with self._lock:
            for record in self._records:
                if record.get("id") == record_id:
                    return dict(record)
        return None

    def remove(self, record_id):
        """按 id 删一条。返回是否真的删掉了（前端据此区分 404）。"""
        rid = str(record_id or "").strip()
        if not rid:
            return False
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.get("id") != rid]
            removed = before - len(self._records)
        if removed:
            self.save()
        return bool(removed)

    def clear_terminal(self):
        """清除所有终态记录（记录天然是终态，所以等于清空）。返回删除条数。"""
        with self._lock:
            removed = len(self._records)
            self._records = []
        if removed:
            self.save()
        return removed

    def counts(self):
        with self._lock:
            out = {"total": len(self._records)}
            for record in self._records:
                status = record.get("status", "?")
                out[status] = out.get(status, 0) + 1
        return out
