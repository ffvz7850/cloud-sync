# -*- coding: utf-8 -*-
"""hasher（SHA-256）与 OpenList StaticHash 的正确性。

SHA-256 用的是 NIST 公开测试向量，纯 Python 实现与 hashlib（如果有）必须一致。
StaticHash 是 OpenList 登录的关键 —— 盐和拼接方式错一个字符就登不上。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))

import hasher
import openlist


class Sha256Test(unittest.TestCase):
    def test_nist_vector_abc(self):
        self.assertEqual(
            hasher.sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_nist_vector_empty(self):
        self.assertEqual(
            hasher.sha256_hex(""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_nist_vector_long(self):
        # 56 字节，正好跨一个填充边界
        msg = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        self.assertEqual(
            hasher.sha256_hex(msg),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")

    def test_bytes_and_str_equivalent(self):
        self.assertEqual(hasher.sha256_hex("hello"), hasher.sha256_hex(b"hello"))

    def test_pure_python_matches_hashlib(self):
        """纯 Python 实现必须与标准答案一致（飞牛上没有 hashlib 时就靠它）。"""
        for msg in (b"", b"abc", b"x" * 1000, bytes(range(256))):
            self.assertEqual(hasher._sha256_pure(msg).hex(), hasher.sha256_hex(msg))

    def test_binary_data(self):
        data = bytes(range(256)) * 10
        self.assertEqual(len(hasher.sha256(data)), 32)


class StaticHashTest(unittest.TestCase):
    def test_salt_is_alist_not_openlist(self):
        """盐必须是 alist 的旧盐，写成 openlist 会永远登不上。"""
        self.assertEqual(openlist.STATIC_HASH_SALT,
                         "https://github.com/alist-org/alist")

    def test_static_hash_formula(self):
        # staticHash = sha256_hex(password + "-" + salt)
        expected = hasher.sha256_hex(
            "mypassword" + "-" + "https://github.com/alist-org/alist")
        self.assertEqual(openlist.static_hash("mypassword"), expected)

    def test_static_hash_is_hex_64(self):
        h = openlist.static_hash("whatever")
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class BackendTest(unittest.TestCase):
    def test_backend_name_valid(self):
        self.assertIn(hasher.backend(), ("hashlib(C)", "pure-python"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
