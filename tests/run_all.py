# -*- coding: utf-8 -*-
"""一次跑完所有测试。

    python tests/run_all.py

Python 部分用 unittest 逐个文件跑（每个文件自带 sys.path 处理，互不干扰）；
前端用 jsdom 真跑页面脚本。任何一项失败，整体退出码非 0。
"""

import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TESTS_DIR = os.path.join(ROOT, "tests")


def discover_js_files():
    """自动发现 `tests/test_*.js`（与 Python 测试同理，避免硬编码漏新文件）。"""
    found = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.startswith("test_") and name.endswith(".js"):
            found.append("tests/" + name)
    return found


def discover_py_files():
    """自动发现 `tests/test_*.py`。

    以前这里是硬编码清单，结果新加的 `test_credentials.py`（68 项，含书签脚本的
    JS 注入/转义安全用例）被漏掉，"跑全部测试"其实是绿的假象。
    改成发现式：新加测试文件不需要记得回来改这里。
    """
    found = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.startswith("test_") and name.endswith(".py"):
            found.append("tests/" + name)
    return found


def run(cmd, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    proc = subprocess.run(cmd, cwd=ROOT, env=merged,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode, proc.stdout


def main():
    failures = []
    total = 0

    py_files = discover_py_files()
    if not py_files:
        print("!! 没发现任何 tests/test_*.py")
        return 1

    for rel in py_files:
        print("=" * 70)
        print("运行", rel)
        print("=" * 70)
        code, out = run([sys.executable, rel])
        # 只打印尾部，避免刷屏；失败时把完整输出留出来
        lines = out.strip().splitlines()
        tail = lines[-8:] if code == 0 else lines[-40:]
        print("\n".join(tail))
        if code != 0:
            failures.append(rel)
        else:
            for line in lines:
                if line.startswith("Ran "):
                    total += int(line.split()[1])
        print()

    node = _find_node()
    if node:
        for rel in discover_js_files():
            print("=" * 70)
            print("运行", rel)
            print("=" * 70)
            env = {"NODE_PATH": _node_modules()}
            code, out = run([node, rel], env=env)
            lines = out.strip().splitlines()
            print("\n".join(lines[-12:] if code == 0 else lines[-40:]))
            if code != 0:
                failures.append(rel)
            else:
                for line in lines:
                    if line.startswith("通过 "):
                        total += int(line.split("通过 ")[1].split("，")[0])
            print()
    else:
        print("!! 没找到 node，跳过前端测试")
        failures.append("test_ui.js（未执行）")

    print("=" * 70)
    if failures:
        print("失败：%s" % ", ".join(failures))
        return 1
    print("全部通过（共 %d 项断言）" % total)
    return 0


def _find_node():
    """找 node。优先级：DD_NODE 环境变量 → 本机约定路径 → PATH 里的 node。

    换机器/换环境时不用改这份代码，导出 DD_NODE 指过去即可。
    """
    import shutil
    env = os.environ.get("DD_NODE")
    if env and os.path.exists(env):
        return env
    candidates = [
        os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node",
                     "versions", "22.22.2-3", "node.exe"),
        os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node",
                     "versions", "22.22.2-3", "bin", "node"),
        shutil.which("node"),
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None


def _node_modules():
    """jsdom 等前端测试依赖所在目录。DD_NODE_PATH / NODE_PATH 优先。

    前端用例用 jsdom 真跑页面脚本，所以这个目录必须指对，否则测试会报
    「Cannot find module 'jsdom'」。最省事的准备方式（任意目录都行）：
        npm i jsdom    然后   DD_NODE_PATH=<那个目录>/node_modules python tests/run_all.py
    """
    env = os.environ.get("DD_NODE_PATH") or os.environ.get("NODE_PATH")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node",
                        "workspace", "node_modules")


if __name__ == "__main__":
    sys.exit(main())
