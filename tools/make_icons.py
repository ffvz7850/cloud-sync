# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库，不依赖 Pillow）。

图标源：`tools/assets/icon-3.0-source.png`（2026-11-22 用户指定的新版图标，256×256，
已经是满幅圆角方 —— 图案四边贴边，只有四角是透明圆角）。

流程：解码 → 裁掉透明外边距 → 裁成正方形 → 盒式平均降采样到 64 / 256 → 写 PNG。
256 到 256 是恒等、到 64 是整数倍（4×4），盒式平均边缘最干净，不做插值糊边。

产出四个文件：
    ICON.PNG / ICON_256.PNG            （64 / 256，包根目录，fnpack 校验这两个）
    app/ui/images/icon_64.png 等       （桌面图标，{0} 占位符会替换成 64/256）

注意：3.0 的源图已经不是旧版那种「带 10px 白描边 + 大留白」的设计稿，
而是四边贴边的满幅板（trim 后仍是 256×256、填充率 100%），所以 ZOOM 必须是 1.0 ——
再放大会把圆角切掉、边缘露直角。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import png_util as P

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(HERE, "assets", "icon-3.0-source.png")


def main():
    data = open(SRC, "rb").read()
    w, h, rgba = P.decode_png(data)
    # 先裁掉四周透明留白，图案填满画布 —— 否则视觉上会比系统自带图标小一圈。
    # v2.2 源图本身已满幅，trim 不会再裁（仍是 256×256）。
    tw, th, trimmed = P.trim_alpha(w, h, rgba)
    side, _, sq = P.crop_center_square(tw, th, trimmed)
    ZOOM = 1.0
    if ZOOM != 1.0:
        sq = P.zoom_center(side, side, sq, ZOOM)
    print("源图 %dx%d → 去留白 %dx%d → 裁方 %dx%d → 放大 %.2fx"
          % (w, h, tw, th, side, side, ZOOM))

    targets = [
        (os.path.join(ROOT, "ICON.PNG"), 64),
        (os.path.join(ROOT, "ICON_256.PNG"), 256),
        (os.path.join(ROOT, "app", "ui", "images", "icon_64.png"), 64),
        (os.path.join(ROOT, "app", "ui", "images", "icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        scaled = P.resize_area(side, side, sq, size)
        png = P.encode_png(size, scaled)
        with open(path, "wb") as fh:
            fh.write(png)
        print("生成 %-46s %6d bytes (%dx%d)"
              % (os.path.relpath(path, ROOT), len(png), size, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
