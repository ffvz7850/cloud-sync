# -*- coding: utf-8 -*-
"""纯标准库 PNG 解码（调色板 / RGB / RGBA）+ 缩放 + 编码，用于把设计稿头像转成应用图标。

为什么手写：飞牛依赖的 python312 是裁剪构建，且本机没有 Pillow —— 图标生成必须零第三方依赖。
只支持本工程用到的子集：bitdepth=8、非隔行、colortype 2/6/3(含 tRNS)。
"""

import struct
import zlib


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data):
    """返回 (w, h, rgba bytearray)。仅支持 8-bit、非隔行。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    off = 8
    w = h = bd = ct = inter = None
    plte = trns = None
    idat = bytearray()
    while off < len(data):
        ln = struct.unpack(">I", data[off:off + 4])[0]
        tag = data[off + 4:off + 8]
        chunk = data[off + 8:off + 8 + ln]
        if tag == b"IHDR":
            w, h, bd, ct, _comp, _filt, inter = struct.unpack(">IIBBBBB", chunk)
        elif tag == b"PLTE":
            plte = chunk
        elif tag == b"tRNS":
            trns = chunk
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break
        off += 12 + ln

    assert bd == 8, "仅支持 8-bit"
    assert inter == 0, "不支持隔行"
    assert ct in (2, 3, 6), "仅支持 RGB / RGBA / 调色板"

    channels = {2: 3, 3: 1, 6: 4}[ct]
    stride = w * channels
    raw = zlib.decompress(bytes(idat))

    # 反滤波
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ft == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:    # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ft != 0:
            raise ValueError("未知 filter %d" % ft)
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # 转 RGBA
    if ct == 6:
        return w, h, out
    if ct == 2:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
        return w, h, rgba
    # 调色板
    assert plte is not None
    n = len(plte) // 3
    palette = [(plte[i * 3], plte[i * 3 + 1], plte[i * 3 + 2]) for i in range(n)]
    alphas = list(trns) if trns else []
    rgba = bytearray(w * h * 4)
    for i in range(w * h):
        idx = out[i]
        r, g, b = palette[idx]
        a = alphas[idx] if idx < len(alphas) else 255
        rgba[i * 4:i * 4 + 4] = bytes((r, g, b, a))
    return w, h, rgba


def crop_center_square(w, h, rgba):
    """居中裁成正方形（以宽为准；高多于宽时上下各裁掉多余的 1/2）。"""
    side = min(w, h)
    x0 = (w - side) // 2
    y0 = (h - side) // 2
    out = bytearray(side * side * 4)
    for y in range(side):
        src = ((y + y0) * w + x0) * 4
        out[y * side * 4:(y + 1) * side * 4] = rgba[src:src + side * 4]
    return side, side, out


def zoom_center(w, h, rgba, factor):
    """以中心放大 factor 倍再裁回原尺寸（最近邻采样）。

    用途：设计稿图标圆角半径偏大（~25%），四角切掉太多，视觉上比系统图标
    小一圈。放大后圆角被推出画布、收窄到 ~12%，图案更满、观感与系统图标一致。
    原尺寸与放大后尺寸相同（裁回 w×h），只是内容被放大。
    """
    if factor <= 1.0:
        return bytearray(rgba)
    out = bytearray(w * h * 4)
    cx, cy = w / 2.0, h / 2.0
    for y in range(h):
        sy = int((y - cy) / factor + cy)
        if sy < 0 or sy >= h:
            continue
        row = sy * w * 4
        for x in range(w):
            sx = int((x - cx) / factor + cx)
            if sx < 0 or sx >= w:
                continue
            dst = (y * w + x) * 4
            src = row + sx * 4
            out[dst:dst + 4] = rgba[src:src + 4]
    return out


def trim_alpha(w, h, rgba, threshold=8):
    """裁掉四周的全透明留白，返回非透明像素的外接包围盒。

    设计稿的圆角方块常带透明外边距（实测 512×536 里图案只占 454×454，约 88%），
    直接缩放会让图标视觉上比系统自带图标小一圈 —— 先裁到包围盒再缩放才能对齐大小。
    """
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        base = y * w * 4
        for x in range(w):
            if rgba[base + x * 4 + 3] > threshold:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return w, h, bytearray(rgba)
    cw = maxx - minx + 1
    ch = maxy - miny + 1
    out = bytearray(cw * ch * 4)
    for y in range(ch):
        src = ((y + miny) * w + minx) * 4
        out[y * cw * 4:(y + 1) * cw * 4] = rgba[src:src + cw * 4]
    return cw, ch, out


def resize_area(src_w, src_h, rgba, dst):
    """通用盒式平均缩放（任意比例，用浮点覆盖权重）。预乘 alpha，边缘不发黑。"""
    out = bytearray(dst * dst * 4)
    sx = src_w / dst
    sy = src_h / dst
    for dy in range(dst):
        y0, y1 = dy * sy, (dy + 1) * sy
        iy0, iy1 = int(y0), min(src_h, int(y1) + 1)
        for dx in range(dst):
            x0, x1 = dx * sx, (dx + 1) * sx
            ix0, ix1 = int(x0), min(src_w, int(x1) + 1)
            r = g = b = a = 0
            wsum = 0.0
            for yy in range(iy0, iy1):
                wy = min(y1, yy + 1) - max(y0, yy)
                if wy <= 0:
                    continue
                rowbase = yy * src_w * 4
                for xx in range(ix0, ix1):
                    wx = min(x1, xx + 1) - max(x0, xx)
                    if wx <= 0:
                        continue
                    wgt = wx * wy
                    p = rowbase + xx * 4
                    aa = rgba[p + 3]
                    r += rgba[p] * aa * wgt
                    g += rgba[p + 1] * aa * wgt
                    b += rgba[p + 2] * aa * wgt
                    a += aa * wgt
                    wsum += wgt
            o = (dy * dst + dx) * 4
            if a == 0 or wsum == 0:
                out[o:o + 4] = b"\x00\x00\x00\x00"
            else:
                out[o] = min(255, int(r / a + 0.5))
                out[o + 1] = min(255, int(g / a + 0.5))
                out[o + 2] = min(255, int(b / a + 0.5))
                out[o + 3] = min(255, int(a / wsum + 0.5))
    return out


def encode_png(size, rgba):
    """RGBA(6) 8-bit PNG。逐行 filter=0，zlib 压缩。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
