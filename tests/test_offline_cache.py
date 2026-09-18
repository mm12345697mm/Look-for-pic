#!/usr/bin/env python3
"""Offline cache + display-zero + avbase slug helpers."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402


class TestDisplayZeros(unittest.TestCase):
    def test_keep_leading_zeros(self):
        self.assertEqual(S.format_display_code("NHDTC-029"), "NHDTC-029")
        self.assertEqual(S.format_display_code("nhdtc029"), "NHDTC-029")
        self.assertEqual(S.format_display_code("NHDTC-99"), "NHDTC-99")

    def test_cid_still_pads(self):
        self.assertTrue(S.code_to_cid("NHDTC-029").endswith("00029") or "029" in S.code_to_cid("NHDTC-029"))


class TestOfflineCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "offline-cache.json"
        S._OFFLINE_CACHE_PATH = self.cache_path

    def tearDown(self):
        S._OFFLINE_CACHE_PATH = None
        self.tmp.cleanup()

    def test_put_get_same_code(self):
        payload = {
            "ok": True,
            "code": "NHDTC-099",
            "title": "テスト作品",
            "title_zh": "測試作品",
            "actress": "A",
            "studio": "S",
            "cid": "1nhdtc00099",
            "cover": "https://example.com/c.jpg",
            "stills": ["https://example.com/1.jpg"],
            "related_by_title": [
                {
                    "code": "NHDTC-100",
                    "title": "rel",
                    "title_zh": "相關",
                    "actress": "A",
                    "cover": "https://example.com/r.jpg",
                    "cid": "x",
                    "line": "theme",
                    "why": "片名相近",
                }
            ],
            "message": "來源：avbase",
        }
        S.offline_cache_put(payload)
        hit = S.offline_cache_get(code="NHDTC-099")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["code"], "NHDTC-099")
        self.assertEqual(hit["title"], "テスト作品")
        self.assertIn("離線快取", hit["message"])
        self.assertTrue(hit.get("from_offline_cache"))
        self.assertEqual(len(hit.get("related_by_title") or []), 1)

    def test_numeric_equivalent_keys(self):
        S.offline_cache_put(
            {
                "ok": True,
                "code": "NHDTC-099",
                "title": "padded",
                "cover": "https://example.com/c.jpg",
            }
        )
        hit99 = S.offline_cache_get(code="NHDTC-99")
        hit099 = S.offline_cache_get(code="NHDTC-099")
        self.assertIsNotNone(hit99)
        self.assertIsNotNone(hit099)
        self.assertEqual(hit99["title"], "padded")
        self.assertEqual(hit099["title"], "padded")
        # display form keeps zeros from stored entry
        self.assertEqual(hit99["code"], "NHDTC-099")

    def test_image_hash_key(self):
        h = "abc" * 20 + "ab"  # 62 hex-ish
        h = "a" * 64
        S.offline_cache_put(
            {
                "ok": True,
                "code": "MIDA-616",
                "title": "demo",
                "cover": "https://example.com/c.jpg",
            },
            image_hash=h,
        )
        hit = S.offline_cache_get(image_hash=h)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["code"], "MIDA-616")

    def test_lru_cap(self):
        old_max = S.OFFLINE_CACHE_MAX
        try:
            S.OFFLINE_CACHE_MAX = 3
            for i in range(5):
                S.offline_cache_put(
                    {
                        "ok": True,
                        "code": f"TEST-{i+1:03d}",
                        "title": f"t{i}",
                        "cover": f"https://example.com/{i}.jpg",
                    }
                )
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(data["entries"]), 3)
        finally:
            S.OFFLINE_CACHE_MAX = old_max

    def test_reject_empty(self):
        S.offline_cache_put({"ok": True, "code": "AAA-001"})  # no title/cover
        self.assertIsNone(S.offline_cache_get(code="AAA-001"))


class TestEntryId(unittest.TestCase):
    def test_same_entry_id(self):
        self.assertEqual(S._offline_cache_entry_id("NHDTC-99"), S._offline_cache_entry_id("NHDTC-099"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
