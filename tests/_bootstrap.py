# -*- coding: utf-8 -*-
"""测试引导：把运行环境伪装成飞牛，并把 `app/server` 挂进 sys.path。

必须在 `import config` **之前**调用（`config.data_dir()` 会读环境变量并 makedirs）。
所有测试模块统一 `import _bootstrap`，由它负责这一次性的环境整形。
"""

import os
import shutil
import sys
import tempfile

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
APP_DIR = os.path.join(_ROOT, "app")
SERVER_DIR = os.path.join(APP_DIR, "server")

# 每个进程一份独立数据目录，避免和开发机上的 _devdata 互相污染
DATA_ROOT = os.path.join(tempfile.gettempdir(), "cloud-sync-tests-%d" % os.getpid())


def setup(clean=True):
    if clean and os.path.isdir(DATA_ROOT):
        shutil.rmtree(DATA_ROOT, ignore_errors=True)
    var = os.path.join(DATA_ROOT, "var")
    etc = os.path.join(DATA_ROOT, "etc")
    tmp = os.path.join(DATA_ROOT, "tmp")
    for path in (var, etc, tmp):
        os.makedirs(path, exist_ok=True)

    # 强制覆盖，不 setdefault —— 否则上一次跑残留的变量会让本次走错目录
    os.environ["TRIM_APPDEST"] = APP_DIR
    os.environ["TRIM_PKGVAR"] = var
    os.environ["TRIM_PKGETC"] = etc
    os.environ["TRIM_PKGTMP"] = tmp
    # 本地浏览白名单：只放测试自己的沙盒目录
    os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = DATA_ROOT

    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    return DATA_ROOT


def sandbox(name):
    """在沙盒里开一个子目录（本地浏览白名单内）。"""
    path = os.path.join(DATA_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


def clean_var():
    """清掉可变数据（每次跑用例前调用，保证凭据/配置不串）。"""
    var = os.path.join(DATA_ROOT, "var")
    shutil.rmtree(var, ignore_errors=True)
    os.makedirs(var, exist_ok=True)


setup()
