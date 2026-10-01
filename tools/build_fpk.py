#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛 fnOS .fpk 打包 + 打包前自检（通用版，纯标准库）

    python3 build_fpk.py --project <应用工程目录> [--fnpack <fnpack 可执行文件>]

做四件事：
  1. 把「安装包内容」拷到干净的暂存目录（排除 tests/ tools/ dist/ build/ 等开发文件）
  2. 打包前自检 —— 这些坑在 NAS 上才暴露，本地先拦住：
       · 必需文件是否齐全（对齐官方 fnpack build 的校验）
       · JSON 是否合法（manifest 不是 JSON，是 INI 风格 key=value）
       · shell 脚本是否 LF 且无 BOM（CRLF 到 NAS 上直接 bad interpreter）
       · 图标尺寸是否为 64 / 256
       · manifest 的 appname / desktop_applaunchname / ui 入口 key / 网关前缀 是否互相对得上
       · Python 后端能否编译
  3. 调用 fnpack build
  4. 产物挪到 dist/，并列出包内清单

Windows 上也能跑。找不到 fnpack 时会给出下载地址。
"""

from __future__ import annotations

import argparse
import json
import py_compile
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# .fpk 里实际要装的东西（多余的开发文件不进包）
INCLUDE = ["manifest", "ICON.PNG", "ICON_256.PNG", "config", "cmd", "wizard", "app"]

# 打包前必须存在（对齐官方 fnpack 的校验规则）
REQUIRED = [
    "manifest",
    "ICON.PNG",
    "ICON_256.PNG",
    "config/privilege",
    "config/resource",
    "cmd/main",
    "cmd/install_init",
    "cmd/install_callback",
    "cmd/upgrade_init",
    "cmd/upgrade_callback",
    "cmd/uninstall_init",
    "cmd/uninstall_callback",
    "cmd/config_init",
    "cmd/config_callback",
    "app/ui/config",
    # 注意：wizard/ 要的是「目录存在」，不是「里面有 install/uninstall 文件」。
    # 官方 fnpack 模板给的就是一个空 wizard/ 目录；手动建 wizard/install 并写 {} 反而会让
    # fnpack build 报 `not valid due to JSON format or content validation failure`。
    "wizard",
]

FNpack_URL = "https://developer.fnnas.com/docs/cli/fnpack/"

OK: list[str] = []
BAD: list[str] = []


def ok(msg: str) -> None:
    OK.append(msg)
    print(f"  [OK] {msg}")


def bad(msg: str) -> None:
    BAD.append(msg)
    print(f"  [!!] {msg}")


def read_manifest(path: Path) -> dict[str, str]:
    """manifest 是 INI 风格 key = value（不是 JSON）"""
    fields: dict[str, str] = {}
    if not path.exists():
        return fields
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def png_size(path: Path) -> tuple[int, int] | None:
    """从 PNG 头读出宽高（IHDR 紧跟在 8 字节签名 + 4 字节长度 + 4 字节类型之后）"""
    try:
        raw = path.read_bytes()[:33]
    except OSError:
        return None
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", raw[16:24])
    return int(w), int(h)


def stage_files(project: Path, stage: Path) -> None:
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for name in INCLUDE:
        src = project / name
        if not src.exists():
            bad(f"源目录缺少 {name}")
            continue
        dst = stage / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    # 清掉编辑器 / 运行产生的垃圾
    for junk in list(stage.rglob(".DS_Store")) + list(stage.rglob("__pycache__")):
        shutil.rmtree(junk, ignore_errors=True) if junk.is_dir() else junk.unlink(missing_ok=True)
    for pyc in stage.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def check_stage(stage: Path) -> None:
    print("\n== 打包前自检 ==")

    for rel in REQUIRED:
        (ok if (stage / rel).exists() else bad)(f"存在 {rel}")

    # 图标尺寸
    for rel, want in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
        p = stage / rel
        if not p.exists():
            continue
        size = png_size(p)
        if size is None:
            bad(f"{rel} 不是合法 PNG")
        elif size != (want, want):
            bad(f"{rel} 尺寸应为 {want}x{want}，实际 {size[0]}x{size[1]}")
        else:
            ok(f"{rel} 尺寸正确 ({want}x{want})")

    # JSON 合法性（manifest 不在此列）
    for rel in ("app/ui/config", "config/privilege", "config/resource",
                "wizard/install", "wizard/uninstall", "wizard/upgrade", "wizard/config"):
        p = stage / rel
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
            ok(f"JSON 合法 {rel}")
        except ValueError as exc:
            bad(f"JSON 不合法 {rel}：{exc}")

    # manifest 必填字段
    fields = read_manifest(stage / "manifest")
    for key in ("appname", "version", "display_name", "desc", "source", "platform"):
        if fields.get(key):
            ok(f"manifest.{key} = {fields[key][:40]}")
        else:
            bad(f"manifest 缺少必填字段 {key}")
    if fields.get("version") and not all(c.isdigit() for c in fields["version"].replace(".", "")):
        bad(f"manifest.version 应为 x.y.z 数字形式，实际 {fields['version']}")

    appname = fields.get("appname", "")
    launch = fields.get("desktop_applaunchname", "")
    if appname and launch:
        if launch.startswith(appname):
            ok(f"desktop_applaunchname 以 appname 开头（{launch}）")
        else:
            bad(f"desktop_applaunchname 必须以 appname 开头，实际 {launch}")

    # 桌面入口：key 要对得上 desktop_applaunchname，网关前缀要对得上 appname
    ui_cfg = stage / "app/ui/config"
    if ui_cfg.exists():
        try:
            entries = json.loads(ui_cfg.read_text(encoding="utf-8")).get(".url", {})
            if not entries:
                bad("app/ui/config 里 .url 是空的（应用在桌面上不会有图标）")
            if launch and launch in entries:
                ok(f"桌面入口 {launch} 与 manifest 一致")
            elif launch:
                bad(f"app/ui/config 里找不到入口 {launch}（key 必须等于 desktop_applaunchname）")
            for name, cfg in entries.items():
                if cfg.get("gatewaySocket"):
                    if cfg.get("gatewayPrefix") == f"/app/{appname}":
                        ok(f"网关前缀 {cfg['gatewayPrefix']} 与 appname 一致")
                    else:
                        bad(f"网关前缀 {cfg.get('gatewayPrefix')} 与 appname={appname} 不匹配")
                if cfg.get("gatewayPrefix") and "." in str(cfg.get("gatewayPrefix")):
                    bad("gatewayPrefix 不能包含 '.'")
                if cfg.get("type") not in ("url", "iframe", None):
                    bad(f"入口 type 只能是 url(新标签页) 或 iframe(桌面内嵌窗口)，实际 {cfg.get('type')}")
        except ValueError as exc:
            bad(f"无法解析 app/ui/config：{exc}")

    # shell 脚本必须 LF 且不能有 BOM
    cmd_dir = stage / "cmd"
    if cmd_dir.exists():
        for f in sorted(p for p in cmd_dir.glob("*") if p.is_file()):
            raw = f.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                bad(f"{f.name} 带 UTF-8 BOM")
            elif b"\r\n" in raw:
                bad(f"{f.name} 是 CRLF —— 到 NAS 上会报 bad interpreter")
            else:
                ok(f"{f.name} 换行/BOM 正常")

    # Python 后端能编译（Windows 上 cfile 不能指向 os.devnull）
    for py in sorted(stage.glob("app/**/*.py")):
        try:
            with tempfile.TemporaryDirectory() as td:
                py_compile.compile(str(py), doraise=True, cfile=str(Path(td) / "check.pyc"))
            ok(f"{py.name} 语法正常")
        except py_compile.PyCompileError as exc:
            bad(f"{py.name} 编译失败：{exc}")


def find_fnpack(project: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates = [
        project.parent / "_fnpack" / "fnpack.exe",
        project / "tools" / "fnpack.exe",
        project / "tools" / "fnpack",
        shutil.which("fnpack") or "",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return str(cand)
    print(f"\n找不到 fnpack，请用 --fnpack 指定路径。下载：{FNpack_URL}")
    sys.exit(2)


def fix_modes(fpk: Path) -> None:
    """重写包内权限：目录 755、cmd/* 755、其余 644。

    为什么必须做：**fnpack 在 Windows 上打出来的 `cmd/*` 是 0666，没有可执行位**。
    包能打成功、能装成功，但 `cmd/main start` 会 Permission denied，应用永远起不来，
    `status` 一直返回 1，而且日志里什么都没有——排查起来极费时间。
    fnpack 自己不校验这一点，只能在打包后补一刀。

    在 Linux/macOS 上跑时这一步是幂等的（把已经是 755 的再设成 755）。
    """
    tmp = fpk.with_name(fpk.name + ".tmp")
    with tarfile.open(fpk, "r:*") as src, tarfile.open(tmp, "w:gz") as dst:
        for member in src.getmembers():
            if member.isdir() or member.name == "cmd" or member.name.startswith("cmd/"):
                member.mode = 0o755
            elif member.isreg():
                member.mode = 0o644
            if member.mtime < 0:
                member.mtime = 0  # 负数时间戳有些安装器会报错
            payload = src.extractfile(member) if member.isreg() else None
            dst.addfile(member, payload)
    tmp.replace(fpk)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".", help="应用工程目录（含 manifest 的那一层）")
    ap.add_argument("--fnpack", default=None, help="fnpack 可执行文件路径")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    if not (project / "manifest").exists():
        print(f"这里没有 manifest，不像是 fnOS 应用工程：{project}")
        return 2

    stage = project / "build" / "fpk"
    dist = project / "dist"
    print(f"项目目录：{project}")

    stage_files(project, stage)
    check_stage(stage)

    print("\n== 打包 ==")
    fnpack = find_fnpack(project, args.fnpack)
    print(f"使用 fnpack：{fnpack}")
    # 打包前先记下工程目录里已有的 .fpk —— 那些是历史交付物或源码包里附带的副本，
    # 绝不能当成本次产物：同名时 shutil.move 会把刚打好的新包覆盖成旧包
    # （而且旧包还会被从工程目录里移走，现场被破坏得莫名其妙）。
    preexisting = {p.resolve() for p in project.glob("*.fpk")}
    proc = subprocess.run([fnpack, "build", "--directory", str(stage)],
                          capture_output=True, text=True, cwd=str(stage))
    print(proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode != 0:
        bad(f"fnpack build 失败（退出码 {proc.returncode}）")
        print(proc.stderr)
        return 1

    produced = sorted(stage.glob("*.fpk"))
    if not produced:
        # 退路：fnpack 若把包留在工程目录，只认「新增的」那些
        produced = sorted(p for p in project.glob("*.fpk")
                          if p.resolve() not in preexisting)
    if not produced:
        bad("没有找到生成的 .fpk（暂存目录与工程目录都没有新增产物）")
        return 1

    dist.mkdir(exist_ok=True)
    final = []
    for f in produced:
        target = dist / f.name
        if f.resolve() != target.resolve():
            shutil.move(str(f), str(target))
        final.append(target)
        ok(f"产物 {target.name}（{target.stat().st_size / 1024:.1f} KB）")

    for fpk in final:
        fix_modes(fpk)
        try:
            with tarfile.open(fpk, "r:*") as tf:
                names = sorted(n for n in tf.getnames() if not n.endswith("/"))
                modes = {m.name: m.mode for m in tf.getmembers()}
            main_mode = modes.get("cmd/main")
            if main_mode is not None:
                if main_mode & 0o111:
                    ok(f"cmd/main 有可执行位（{oct(main_mode)}）")
                else:
                    bad(f"cmd/main 没有可执行位（{oct(main_mode)}），装到 NAS 上会 Permission denied")
            print(f"\n== {fpk.name} 内含 {len(names)} 个文件 ==")
            for n in names:
                print("   ", n)
        except tarfile.TarError as exc:
            print(f"无法读取包内容：{exc}")

    print(f"\n自检通过 {len(OK)}，失败 {len(BAD)}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
