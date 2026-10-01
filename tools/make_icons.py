# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库，不依赖 Pillow）。

产出四个文件：
    ICON.PNG / ICON_256.PNG            （64 / 256，包根目录，fnpack 校验这两个）
    app/ui/images/icon_64.png 等       （桌面图标，{0} 占位符会替换成 64/256）

图形：飞牛主蓝渐变底 + 白色云朵 + 向下箭头（云盘 = 云 + 存取）。
"""

import math
import os
import struct
import sys
import zlib

BG_TOP = (38, 129, 255)
BG_BOTTOM = (15, 95, 255)
WHITE = (255, 255, 255)


def rounded_rect_alpha(x, y, w, h, radius, px, py):
    """返回该点在圆角矩形内的覆盖率（0..1），做一点抗锯齿。"""
    cx = min(max(px, radius), w - radius)
    cy = min(max(py, radius), h - radius)
    dx = px - cx
    dy = py - cy
    dist = math.hypot(dx, dy)
    if dist <= radius - 1:
        return 1.0
    if dist >= radius + 1:
        return 0.0
    return (radius + 1 - dist) / 2.0


def cloud_coverage(px, py, size):
    """云朵的覆盖率：三个圆 + 一个底座的**并集**。

    注意底座必须和圆取 `max`（并集），不能直接覆盖 ——
    早先写成覆盖，底座左右两端的半透明像素把圆的实心区域盖掉了，
    渲染出来是两道竖细线。
    """
    s = size / 100.0
    circles = [
        (36 * s, 56 * s, 17 * s),
        (53 * s, 46 * s, 21 * s),
        (70 * s, 57 * s, 14 * s),
    ]
    best = 0.0
    for cx, cy, r in circles:
        d = math.hypot(px - cx, py - cy)
        if d <= r - 1:
            best = max(best, 1.0)
        elif d < r + 1:
            best = max(best, (r + 1 - d) / 2.0)

    # 底座（与圆求并集）
    left, right, top, bottom = 34 * s, 72 * s, 56 * s, 70 * s
    if left - 1 <= px <= right + 1 and top - 1 <= py <= bottom + 1:
        cx = min(max(px, left + 1), right - 1)
        cy = min(max(py, top + 1), bottom - 1)
        dist = math.hypot(px - cx, py - cy)
        base = 1.0 if dist <= 1.0 else (2.0 - dist)
        best = max(best, max(0.0, min(1.0, base)))
    return max(0.0, min(1.0, best))


def arrow_coverage(px, py, size):
    """向下箭头（在云朵下方）。"""
    s = size / 100.0
    cx = 53 * s
    top = 74 * s
    shaft_w = 5 * s
    shaft_bottom = 86 * s
    head_w = 13 * s
    head_top = 82 * s

    inside = False
    if abs(px - cx) <= shaft_w and top <= py <= shaft_bottom:
        inside = True
    if head_top <= py <= 94 * s:
        t = (py - head_top) / max(1e-6, (94 * s - head_top))
        half = head_w * (1 - t)
        if abs(px - cx) <= half:
            inside = True
    return 1.0 if inside else 0.0


def render(size):
    pixels = bytearray()
    for y in range(size):
        pixels.append(0)  # filter type 0
        for x in range(size):
            px = x + 0.5
            py = y + 0.5
            bg_a = rounded_rect_alpha(0, 0, size, size, size * 0.22, px, py)
            if bg_a <= 0:
                pixels.extend((0, 0, 0, 0))
                continue
            ratio = y / max(1, size - 1)
            base = tuple(
                int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
            )
            mark = min(1.0, cloud_coverage(px, py, size) + arrow_coverage(px, py, size))
            color = tuple(int(base[i] * (1 - mark) + WHITE[i] * mark) for i in range(3))
            alpha = int(round(255 * bg_a))
            pixels.extend((color[0], color[1], color[2], alpha))
    return bytes(pixels)


def write_png(path, size, raw):
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return len(png)


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    targets = [
        (os.path.join(root, "ICON.PNG"), 64),
        (os.path.join(root, "ICON_256.PNG"), 256),
        (os.path.join(root, "app", "ui", "images", "icon_64.png"), 64),
        (os.path.join(root, "app", "ui", "images", "icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        written = write_png(path, size, render(size))
        print("生成 %-46s %4d bytes (%dx%d)" % (os.path.relpath(path, root), written, size, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
