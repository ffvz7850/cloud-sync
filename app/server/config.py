# -*- coding: utf-8 -*-
"""路径解析与配置读写。

飞牛的目录约定（见 fpk 规范）：
    app/ 内容      → $TRIM_APPDEST
    etc            → $TRIM_PKGETC
    var            → $TRIM_PKGVAR   ← 可变数据、日志、凭据都放这里
    tmp            → $TRIM_PKGTMP

脱离飞牛（本机开发）时全部退到工程下的 `_devdata/`，这样测试不需要伪造环境变量。
"""

import copy
import json
import os
import tempfile
import threading

# 应用自身标识，前端探测基地址时会核对它
APP_NAME = "cloud-sync"
APP_VERSION = "2.15.39"

DEFAULT_OPENLIST_URL = "http://192.168.0.10:5244"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def _env(name, default=""):
    return os.environ.get(name, default).strip()


def project_root():
    """app/server/ 往上两级。"""
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def is_inside_trim():
    return bool(_env("TRIM_APPDEST"))


def app_dest():
    return _env("TRIM_APPDEST") or os.path.join(project_root(), "app")


def data_dir():
    """可变数据目录（config.json / credentials.json / info.log）。"""
    path = _env("TRIM_PKGVAR") or os.path.join(project_root(), "_devdata")
    os.makedirs(path, exist_ok=True)
    return path


def etc_dir():
    path = _env("TRIM_PKGETC") or os.path.join(project_root(), "_devdata", "etc")
    os.makedirs(path, exist_ok=True)
    return path


def tmp_dir():
    path = _env("TRIM_PKGTMP") or os.path.join(project_root(), "_devdata", "tmp")
    os.makedirs(path, exist_ok=True)
    return path


def www_dir():
    return os.path.join(app_dest(), "www")


def settings_path():
    return os.path.join(etc_dir(), "settings.conf")


def config_path():
    return os.path.join(data_dir(), "config.json")


def credentials_path():
    return os.path.join(data_dir(), "credentials.json")


def log_path():
    return os.path.join(data_dir(), "info.log")


def accessible_paths():
    """允许本地浏览的根目录白名单。

    `TRIM_DATA_ACCESSIBLE_PATHS` 是**路径列表**，分隔符要走 os.pathsep，
    写死 ':' 会在开发机的盘符上被切断。
    """
    raw = _env("TRIM_DATA_ACCESSIBLE_PATHS")
    if raw:
        parts = [p for p in raw.split(os.pathsep) if p.strip()]
        if parts:
            return [os.path.abspath(p) for p in parts]

    # 飞牛的存储空间固定挂在这个前缀下
    vols = []
    for base in ("/vol1", "/vol2", "/vol3", "/vol4"):
        if os.path.isdir(base):
            vols.append(base)
    if vols:
        return vols
    return [os.path.abspath(os.sep)]


# --------------------------------------------------------------------------
# settings.conf（安装/配置阶段写入，值里可能有空格，不能 source）
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 小文件读取缓存（settings.conf / config.json / 凭据共用）
# --------------------------------------------------------------------------
# 为什么需要：`_raw_request()` 每发一次 OpenList 请求都要读一遍凭据文件（拿
# baseUrl 和 token 各一次）；`load_config()` 每次又要读一遍 config.json 加两遍
# settings.conf。云端扫描是「每目录一次请求」，几百个目录就是上千次「打开 + 解析
# JSON」，纯属白烧 IO —— 扫描慢有很大一部分耗在这里。
#
# 做法：按 (mtime_ns, size) 判断文件是否变化，没变就复用上次解析结果。
# 写入路径（atomic_write_json）会主动失效缓存，不依赖时间戳精度。

_FILE_CACHE = {}            # path -> (signature, parsed_value)
_FILE_CACHE_LOCK = threading.Lock()


def _stat_signature(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def invalidate_file_cache(path=None):
    """让某个文件（或全部）的缓存失效。写入后必须调用。"""
    with _FILE_CACHE_LOCK:
        if path is None:
            _FILE_CACHE.clear()
        else:
            _FILE_CACHE.pop(path, None)


def read_cached_json(path, default=None):
    """带缓存的「读文件 + 解析 JSON」。

    返回的一定是**深拷贝**，调用方随便改都不会污染缓存。
    文件不存在或解析失败时返回 `default`（默认 `{}`）并把结果也缓存住 ——
    否则「文件还没建」这种情况每次都要重试一遍 IO。
    """
    fallback = {} if default is None else default
    signature = _stat_signature(path)
    with _FILE_CACHE_LOCK:
        hit = _FILE_CACHE.get(path)
        if hit is not None and hit[0] == signature:
            return copy.deepcopy(hit[1])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = fallback
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[path] = (signature, data)
    return copy.deepcopy(data)


def read_cached_lines(path):
    """带缓存的「按行读文本」，返回行列表（settings.conf 用）。"""
    signature = _stat_signature(path)
    with _FILE_CACHE_LOCK:
        hit = _FILE_CACHE.get(path)
        if hit is not None and hit[0] == signature:
            return list(hit[1])
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:
        lines = []
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[path] = (signature, lines)
    return list(lines)


def read_settings():
    out = {}
    path = settings_path()
    if not os.path.isfile(path):
        return out
    try:
        for line in read_cached_lines(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    except Exception:
        return out
    return out


def setting(key, default=""):
    """按 settings.conf > 环境变量 > 默认值 的优先级取值。"""
    conf = read_settings()
    if key in conf and conf[key] != "":
        return conf[key]
    env = _env(key)
    if env:
        return env
    return default


def setting_int(key, default):
    raw = setting(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def setting_bool(key, default):
    raw = setting(key, "")
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# config.json（业务配置，界面里改）
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "localRoot": "",           # 本地默认根目录（左侧目录树的默认起点）
    "cloudRoot": "/",          # 云端默认根目录（右侧目录树的默认起点）
    "scanDepth": 3,            # 目录对比递归深度（0 = 只看当前层）
    "scanConcurrency": 8,      # 云端扫描并发路数（每层同时列几个目录）
    "excludeNames": [          # 对比时跳过的名字（大小写不敏感，支持 * 通配）
        ".DS_Store", "Thumbs.db", "desktop.ini", "*.tmp", "*.part", "*.ddpart",
        "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
    ],
    "uploadConcurrency": 2,    # 同时上传几个文件
    "skipExisting": True,      # 上传前发现云端已有同大小文件则跳过（避免重复传）
    "verifyAfterUpload": True, # 上传完成后读回云端比对大小
    "maxAttempts": 3,          # 单个文件失败自动重试次数
    "autoScan": False,         # 绑定页是否自动定时全量扫描
    "fullScanIntervalDays": 7,   # 全量扫描间隔（天），默认 7 天
    "scanScheduleTime": "03:00", # 扫描执行时间点（HH:MM），默认凌晨3点
    "bindingScanConcurrency": 2, # 全局绑定扫描并发数（同时最多几个绑定在扫描），默认2
    "localSourceMode": "fs",   # 文件传输左侧来源：fs=本机文件系统，cloud=OpenList 路径
    "localSourceRoot": "",     # cloud 模式下左侧起始 OpenList 路径
}


def _deep_merge(base, incoming):
    out = dict(base)
    for key, value in (incoming or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    data = read_cached_json(config_path(), {})
    cfg = _deep_merge(DEFAULT_CONFIG, data if isinstance(data, dict) else {})
    # settings.conf 里显式指定的值优先级最高
    if setting("OPENLIST_URL", ""):
        cfg["openlistUrl"] = setting("OPENLIST_URL", "")
    if setting("UPLOAD_CONCURRENCY", ""):
        cfg["uploadConcurrency"] = setting_int("UPLOAD_CONCURRENCY", cfg["uploadConcurrency"])
    return cfg


def save_config(cfg):
    atomic_write_json(config_path(), cfg)
    return cfg


def atomic_write_json(path, payload):
    """原子写：先写同目录临时文件再 os.replace（避免半截文件）。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    # 不靠时间戳精度：写完立刻让缓存失效，下一次读一定是新内容
    invalidate_file_cache(path)
    return path


def atomic_write_text(path, text, mode=0o600):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    invalidate_file_cache(path)
    return path
