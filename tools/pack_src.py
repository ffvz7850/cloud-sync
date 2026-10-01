#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把工程打成交接用的 src.zip（纯标准库，Windows / Linux 都能跑）。

    python tools/pack_src.py                 # 输出 dist/doubao-drive-<版本>-src.zip
    python tools/pack_src.py --list          # 只列出会打进去的文件，不打包
    python tools/pack_src.py --out 某个.zip   # 指定输出路径

三个必须显式处理、否则会坑到接手人的点：

1. **空目录** —— zip 格式本身不记录目录，`wizard/` 这种空目录不打条目就会丢，
   解压后 `tools/build_fpk.py` 的自检直接报「缺少 wizard/」。所以目录要主动写条目
   （名字以 / 结尾）。
2. **中文文件名** —— `docs/API参考.md` 这类名字要显式设 UTF-8 标志位（0x800），
   否则部分解压工具会按 cp437 解码成乱码。
3. **权限位** —— Unix 模式放在 external_attr 的高 16 位。不写的话解压出来
   `cmd/*` 没有可执行位，装到 NAS 上应用起不来。目录还要带 DOS 目录位 0x10。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 本脚本在 tools/ 下，工程根在上一级

# 构建/缓存目录不进包
SKIP_DIRS = {"build", "dist", "_devdata", "__pycache__", ".git", ".idea", ".mutbak"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}

# 工程根下这些是「历史遗留的重复件或旧产物」，不进交接包（内容在别处有）：
#   · 2.9.x 的说明文档 → 已在 docs/历史说明-*.md 各存一份，当前基线留在根目录
#   · doubao-drive.fpk → 旧版本残留产物（2.8.9），当前交付物在 dist/ 里现打
SKIP_RELS = {
    "doubao-drive-2.9.1-说明.md",
    "doubao-drive-2.9.2-说明.md",
    "doubao-drive-2.9.3-说明.md",
    "doubao-drive.fpk",
}

# 这些目录下按可执行文件打包
EXEC_DIRS = ("cmd",)
# 另外这几个单文件也要可执行位（接手方直接 ./tools/xxx.py 就能跑）
EXEC_FILES = {"tools/build_fpk.py", "tools/mutcheck.py", "tools/pack_src.py",
              "tools/make_icons.py", "tools/fnpack.exe"}

# 交接包必须包含这些（少了接手方会缺胳膊少腿）
REQUIRED = [
    "manifest", "README.md", "ICON.PNG", "ICON_256.PNG",
    "app/server/main.py", "app/server/http_api.py", "app/www/index.html",
    "cmd/main", "config/privilege",
    "tests/run_all.py", "tests/smoke_e2e.py", "tests/test_ui.js",
    "tools/build_fpk.py", "tools/mutcheck.py", "tools/pack_src.py",
    "docs/交接说明.md",
]


def make_info(arcname: str, is_dir: bool, executable: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arcname + "/" if is_dir else arcname)
    zi.date_time = time.localtime()[:6]
    # 显式声明文件名是 UTF-8：中文名不会在解压时变成乱码
    zi.flag_bits |= 0x800
    mode = 0o40755 if is_dir else (0o100755 if executable else 0o100644)
    zi.external_attr = (mode << 16) | (0x10 if is_dir else 0)
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def collect(root: Path) -> list[tuple[str, bool, bool]]:
    entries: list[tuple[str, bool, bool]] = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_root = os.path.relpath(cur, root).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        if rel_root and rel_root not in SKIP_RELS:
            entries.append((rel_root, True, False))          # 目录条目（含空目录）
        for name in sorted(files):
            if name in SKIP_NAMES or name.endswith(".pyc"):
                continue
            rel = (rel_root + "/" + name) if rel_root else name
            if rel in SKIP_RELS:
                continue
            exec_bit = rel.split("/")[0] in EXEC_DIRS or rel in EXEC_FILES
            entries.append((rel, False, exec_bit))
    return entries


def version_of(root: Path) -> str:
    man = root / "manifest"
    if man.exists():
        for line in man.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip()
    return "0.0.0"


def verify(zip_path: Path, root: Path) -> list[str]:
    """回读校验：目录条目、中文名 UTF-8 标志、权限位、必需文件齐全。"""
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        names = {i.filename for i in infos}
        dirs = [i.filename for i in infos if i.is_dir()]
        cjk = [i for i in infos if any(ord(c) > 127 for c in i.filename)]
        modes = {i.filename: (i.external_attr >> 16) for i in infos}

    for rel in REQUIRED:
        if rel + "/" not in names and rel not in names:
            problems.append("缺少 %s" % rel)
    if "wizard/" not in dirs:
        problems.append("wizard/ 空目录条目缺失")
    if not cjk:
        problems.append("没有中文文件名（docs/ 下应该有）")
    if any(not (i.flag_bits & 0x800) for i in cjk):
        problems.append("中文文件名未设 UTF-8 标志位")
    for rel in ("cmd/main", "cmd/install_init", "tools/build_fpk.py",
                "tools/mutcheck.py", "tools/fnpack.exe"):
        if rel in modes and not (modes[rel] & 0o111):
            problems.append("%s 缺可执行位" % rel)
    if (root / "app/server/__pycache__").exists() and any(
            "__pycache__" in n for n in names):
        problems.append("把 __pycache__ 打进去了")

    print("\n== 回读校验 ==")
    print("  条目 %d 个（其中目录 %d 个，中文名 %d 个）" % (len(infos), len(dirs), len(cjk)))
    print("  cmd/main 权限：%o" % modes.get("cmd/main", 0))
    print("  tools/fnpack.exe 权限：%o" % modes.get("tools/fnpack.exe", 0))
    for p in problems:
        print("  [!!] %s" % p)
    if not problems:
        print("  校验通过")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="工程根目录")
    ap.add_argument("--out", default=None, help="输出 zip 路径")
    ap.add_argument("--list", action="store_true", help="只列出会打进包的文件")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not (root / "manifest").exists():
        print("这里没有 manifest，不像工程根目录：%s" % root)
        return 2

    entries = collect(root)
    ver = version_of(root)
    out = Path(args.out) if args.out else root / "dist" / ("doubao-drive-%s-src.zip" % ver)

    if args.list:
        for arcname, is_dir, exec_bit in entries:
            if is_dir:
                continue
            print("  %s%s" % (arcname, "  [x]" if exec_bit else ""))
        print("\n共 %d 个文件（%d 个条目含目录）" % (
            sum(1 for _, d, _ in entries if not d), len(entries)))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, is_dir, exec_bit in entries:
            zi = make_info(arcname, is_dir, exec_bit)
            if is_dir:
                zf.writestr(zi, b"")
            else:
                zf.writestr(zi, (root / arcname).read_bytes())

    files = sum(1 for _, d, _ in entries if not d)
    print("打包完成：%s" % out)
    print("  版本 %s；%d 个文件，%.1f KB"
          % (ver, files, out.stat().st_size / 1024.0))

    problems = verify(out, root)

    # 顺手比一下：zip 里的每个文件与工程源码逐字节一致（写错路径/编码会在这里露出来）
    with zipfile.ZipFile(out) as zf:
        bad = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            src = root / info.filename
            if not src.exists() or src.read_bytes() != zf.read(info.filename):
                print("  [!!] 内容不一致：%s" % info.filename)
                bad += 1
    print("  逐字节核对：%d 个不一致" % bad)
    return 1 if (problems or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
