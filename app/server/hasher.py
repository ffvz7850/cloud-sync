# -*- coding: utf-8 -*-
"""SHA-256，用于 OpenList 登录的 StaticHash。

飞牛自带 python312 是裁剪构建，连 `hashlib` 这种「看起来肯定有」的模块都可能缺
C 扩展后端（实测它缺 `_lzma`）。所以这里：**先功能性探测 hashlib，探不到就用
纯 Python 实现兜底**。探测必须真算一次，不能只看能不能 import —— 有些裁剪构建
能 import 但调用时才炸。

只需要 SHA-256 这一个算法，所以从原 crypto.py 里抽出最小子集，不再拖着整套
P-256/ECDSA（那是给豆包 DPoP 用的，OpenList 用不上）。
"""

import struct

_MASK32 = 0xFFFFFFFF

# 软导入 + 功能性探测：真算一次，确认 C 后端可用
_FAST = None
try:
    import hashlib as _hashlib

    if len(_hashlib.sha256(b"abc").hexdigest()) == 64:
        _FAST = _hashlib.sha256
except Exception:  # pragma: no cover - 取决于运行环境
    _FAST = None


_K = (
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1,
    0x923F82A4, 0xAB1C5ED5, 0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
    0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174, 0xE49B69C1, 0xEFBE4786,
    0x0FC19DC6, 0x240CA1CC, 0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7, 0xC6E00BF3, 0xD5A79147,
    0x06CA6351, 0x14292967, 0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
    0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85, 0xA2BFE8A1, 0xA81A664B,
    0xC24B8B70, 0xC76C51A3, 0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5, 0x391C0CB3, 0x4ED8AA4A,
    0x5B9CCA4F, 0x682E6FF3, 0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
    0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
)


def _rotr(x, n):
    return ((x >> n) | (x << (32 - n))) & _MASK32


def _construct_words(chunk):
    w = list(struct.unpack(">16I", chunk))
    for i in range(16, 64):
        s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
        s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
        w.append((w[i - 16] + s0 + w[i - 7] + s1) & _MASK32)
    return w


def _sha256_pure(data):
    h = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
         0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19]
    bitlen = len(data) * 8
    data = data + b"\x80"
    while len(data) % 64 != 56:
        data += b"\x00"
    data += struct.pack(">Q", bitlen)
    for off in range(0, len(data), 64):
        w = _construct_words(data[off:off + 64])
        a, b, c, d, e, f, g, hh = h
        for i in range(64):
            s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ ((~e & _MASK32) & g)
            t1 = (hh + s1 + ch + _K[i] + w[i]) & _MASK32
            s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            t2 = (s0 + maj) & _MASK32
            hh, g, f, e = g, f, e, (d + t1) & _MASK32
            d, c, b, a = c, b, a, (t1 + t2) & _MASK32
        h = [(x + y) & _MASK32 for x, y in zip(h, (a, b, c, d, e, f, g, hh))]
    return struct.pack(">8I", *h)


def sha256(data):
    """返回 32 字节摘要。有 C 后端就走 C 后端。"""
    if _FAST is not None:
        return _FAST(data).digest()
    return _sha256_pure(data)


def sha256_hex(data):
    """返回小写十六进制摘要字符串。data 可以是 bytes 或 str（按 utf-8 编码）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if _FAST is not None:
        return _FAST(data).hexdigest()
    return _sha256_pure(data).hex()


def backend():
    """诊断用：当前走的是 C 后端还是纯 Python。"""
    return "hashlib(C)" if _FAST is not None else "pure-python"
