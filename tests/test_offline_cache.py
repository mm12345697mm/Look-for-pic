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
        self.assertEqual(S.format_display_code("DOSD-008"), "DOSD-008")

    def test_cid_still_pads(self):
        self.assertTrue(S.code_to_cid("NHDTC-029").endswith("00029") or "029" in S.code_to_cid("NHDTC-029"))
        self.assertEqual(S.code_to_cid("DOSD-008"), "dosd00008")

    def test_prefer_keeps_query_zeros(self):
        self.assertEqual(S.prefer_display_code("DOSD-008", "DOSD-8"), "DOSD-008")
        self.assertEqual(S.prefer_display_code("NHDTC-99", "NHDTC-099"), "NHDTC-099")
        self.assertTrue(S.codes_numeric_equal("DOSD-008", "DOSD-8"))


class TestCoverAndSlugVariants(unittest.TestCase):
    def test_lookup_slugs_include_stripped_and_display(self):
        slugs = S.code_lookup_slugs("DOSD-008")
        self.assertEqual(slugs[0], "DOSD-008")
        self.assertIn("DOSD-8", slugs)
        self.assertIn("dosd-008", slugs)
        self.assertEqual(S.code_stripped_form("DOSD-008"), "DOSD-8")
        self.assertIsNone(S.code_stripped_form("DOSD-8"))

    def test_cid_candidates_include_stripped_pads(self):
        cids = S.cover_cid_candidates("DOSD-008")
        for expected in ("dosd00008", "1dosd00008", "dosd008", "dosd8", "dosd08"):
            self.assertIn(expected, cids, msg=f"missing {expected} in {cids}")
        # Display must stay padded even while CID guesses strip/pad
        self.assertEqual(S.format_display_code("DOSD-008"), "DOSD-008")

    def test_now_printing_not_usable(self):
        self.assertFalse(S.usable_cover_url(""))
        self.assertFalse(S.usable_cover_url(None))
        self.assertFalse(
            S.usable_cover_url(
                "https://pics.dmm.co.jp/digital/video/dosd00008/now_printing.jpg"
            )
        )
        self.assertFalse(
            S.usable_cover_url(
                "https://imgsrc.dmm.com/pics/mono/movie/n/now_printing/now_printing.jpg"
            )
        )
        self.assertTrue(S.usable_cover_url("https://example.com/c.jpg"))
        self.assertFalse(S._offline_cache_payload_ok({
            "ok": True,
            "code": "DOSD-008",
            "title": "gemini only",
            "cover": None,
        }))
        self.assertFalse(S._offline_cache_payload_ok({
            "ok": True,
            "code": "DOSD-008",
            "title": "gemini only",
            "cover": "https://pics.dmm.co.jp/x/now_printing.jpg",
        }))
        self.assertTrue(S._offline_cache_payload_ok({
            "ok": True,
            "code": "DOSD-008",
            "title": None,
            "cover": "https://example.com/c.jpg",
        }))


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

    def test_reject_title_only(self):
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DOSD-008",
                "title": "weak gemini title",
                "cid": None,
                "cover": None,
                "stills": [],
            }
        )
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))
        self.assertFalse(self.cache_path.is_file() and self.cache_path.stat().st_size > 2)

    def test_reject_now_printing_cover(self):
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DOSD-008",
                "title": "has placeholder cover",
                "cid": "dosd00008",
                "cover": "https://pics.dmm.co.jp/digital/video/dosd00008/now_printing.jpg",
            }
        )
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))

    def test_get_purges_incomplete_poison(self):
        """Old cache files may store title-only; get must miss and delete them."""
        eid = S._offline_cache_entry_id("DOSD-008")
        data = {
            "version": 1,
            "by_key": {
                "code:DOSD-008": eid,
                "code_num:DOSD-8": eid,
            },
            "entries": {
                eid: {
                    "ok": True,
                    "code": "DOSD-008",
                    "title": "gemini title only",
                    "cover": "",
                    "stills": [],
                    "message": "來源：gemini",
                    "touched_at": 1.0,
                }
            },
        }
        self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))
        saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertNotIn(eid, saved.get("entries") or {})
        self.assertNotIn("code:DOSD-008", saved.get("by_key") or {})

    def test_get_purges_now_printing_entry(self):
        eid = S._offline_cache_entry_id("DOSD-008")
        data = {
            "version": 1,
            "by_key": {"code:DOSD-008": eid},
            "entries": {
                eid: {
                    "ok": True,
                    "code": "DOSD-008",
                    "title": "t",
                    "cover": "https://imgsrc.dmm.com/pics/mono/movie/n/now_printing/now_printing.jpg",
                    "touched_at": 1.0,
                }
            },
        }
        self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))
        saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertNotIn(eid, saved.get("entries") or {})

    def test_put_does_not_overwrite_good_cover_with_title_only(self):
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DOSD-008",
                "title": "real",
                "cover": "https://example.com/real.jpg",
            }
        )
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DOSD-008",
                "title": "gemini retry",
                "cover": None,
            }
        )
        hit = S.offline_cache_get(code="DOSD-008")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["cover"], "https://example.com/real.jpg")
        self.assertEqual(hit["title"], "real")


class TestOfflineCacheChineseTitles(unittest.TestCase):
    """Cache hits must still fill title_zh on main + related, then merge-put."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "offline-cache.json"
        S._OFFLINE_CACHE_PATH = self.cache_path

    def tearDown(self):
        S._OFFLINE_CACHE_PATH = None
        self.tmp.cleanup()

    def _stored_payload(self, **extra):
        base = {
            "ok": True,
            "code": "NHDTC-099",
            "title": "日本語タイトル",
            "cover": "https://example.com/c.jpg",
            "stills": ["https://example.com/1.jpg"],
            "related_by_title": [
                {
                    "code": "NHDTC-100",
                    "title": "関連作",
                    "cover": "https://example.com/r.jpg",
                    "cid": "x",
                    "line": "theme",
                    "why": "片名相近",
                }
            ],
        }
        base.update(extra)
        return base

    def _fake_resolve(self, code, title_ja=None, existing_zh=None, user_title=None):
        c = str(code or "")
        if c in ("NHDTC-099", "NHDTC-99"):
            return "中文主標"
        if c in ("NHDTC-100", "NHDTC-0100"):
            return "中文相關"
        return None

    def test_enrich_fills_main_and_related_then_puts(self):
        S.offline_cache_put(self._stored_payload())
        hit = S.offline_cache_get(code="NHDTC-099")
        self.assertIsNotNone(hit)
        self.assertFalse(hit.get("title_zh"))
        self.assertFalse((hit.get("related_by_title") or [{}])[0].get("title_zh"))
        with mock.patch.object(S, "resolve_chinese_title", side_effect=self._fake_resolve):
            out = S.enrich_offline_cache_hit(hit)
        self.assertEqual(out.get("title_zh"), "中文主標")
        self.assertEqual(out["related_by_title"][0].get("title_zh"), "中文相關")
        self.assertEqual(out.get("cover"), "https://example.com/c.jpg")
        self.assertEqual(out.get("stills"), ["https://example.com/1.jpg"])
        # Next get already has Chinese without another network fetch
        with mock.patch.object(
            S, "resolve_chinese_title", side_effect=AssertionError("cache should already have zh")
        ):
            hit2 = S.offline_cache_get(code="NHDTC-099")
        self.assertEqual(hit2.get("title_zh"), "中文主標")
        self.assertEqual(hit2["related_by_title"][0].get("title_zh"), "中文相關")
        self.assertEqual(hit2.get("cover"), "https://example.com/c.jpg")
        self.assertEqual(hit2.get("stills"), ["https://example.com/1.jpg"])

    def test_identify_code_cache_hit_no_longer_skips_zh(self):
        S.offline_cache_put(self._stored_payload())
        with mock.patch.object(S, "resolve_chinese_title", side_effect=self._fake_resolve):
            out = S.identify_code("NHDTC-099")
        self.assertTrue(out.get("from_offline_cache"))
        self.assertEqual(out.get("title_zh"), "中文主標")
        rel = out.get("related_by_title") or []
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0].get("title_zh"), "中文相關")

    def test_pipeline_manual_code_cache_hit_enriches(self):
        S.offline_cache_put(self._stored_payload())
        with mock.patch.object(S, "resolve_chinese_title", side_effect=self._fake_resolve):
            result, status = S.run_identify_pipeline(
                image_bytes=None,
                filename=None,
                user_code="NHDTC-099",
            )
        self.assertEqual(status, 200)
        self.assertTrue(result.get("from_offline_cache"))
        self.assertEqual(result.get("title_zh"), "中文主標")
        self.assertEqual(result["related_by_title"][0].get("title_zh"), "中文相關")
        # cover gate still holds
        self.assertTrue(S.usable_cover_url(result.get("cover")))

    def test_put_merges_related_title_zh_without_wiping_cover(self):
        S.offline_cache_put(self._stored_payload(title_zh="中文主標"))
        # Simulate a thinner put (related missing zh, same cover)
        S.offline_cache_put(
            {
                "ok": True,
                "code": "NHDTC-099",
                "title": "日本語タイトル",
                "title_zh": "中文主標",
                "cover": "https://example.com/c.jpg",
                "stills": ["https://example.com/1.jpg"],
                "related_by_title": [
                    {
                        "code": "NHDTC-100",
                        "title": "関連作",
                        "cover": "https://example.com/r.jpg",
                        "line": "theme",
                    }
                ],
            }
        )
        # Inject zh on related via first put then merge
        S.offline_cache_put(
            {
                "ok": True,
                "code": "NHDTC-099",
                "title": "日本語タイトル",
                "cover": "https://example.com/c.jpg",
                "related_by_title": [
                    {
                        "code": "NHDTC-100",
                        "title": "関連作",
                        "title_zh": "中文相關",
                        "cover": "https://example.com/r.jpg",
                        "line": "theme",
                    }
                ],
            }
        )
        thinner = {
            "ok": True,
            "code": "NHDTC-099",
            "title": "日本語タイトル",
            "cover": "https://example.com/c.jpg",
            "related_by_title": [
                {
                    "code": "NHDTC-100",
                    "title": "関連作",
                    "cover": "https://example.com/r.jpg",
                    "line": "theme",
                }
            ],
        }
        S.offline_cache_put(thinner)
        hit = S.offline_cache_get(code="NHDTC-099")
        self.assertEqual(hit.get("cover"), "https://example.com/c.jpg")
        self.assertEqual(hit["related_by_title"][0].get("title_zh"), "中文相關")
        self.assertEqual(hit.get("stills"), ["https://example.com/1.jpg"])

    def test_enrich_skips_network_when_already_filled(self):
        payload = self._stored_payload(title_zh="已有中文")
        payload["related_by_title"][0]["title_zh"] = "相關中文"
        S.offline_cache_put(payload)
        hit = S.offline_cache_get(code="NHDTC-099")
        with mock.patch.object(
            S, "resolve_chinese_title", side_effect=AssertionError("should not fetch")
        ):
            out = S.enrich_offline_cache_hit(hit)
        self.assertEqual(out.get("title_zh"), "已有中文")
        self.assertEqual(out["related_by_title"][0].get("title_zh"), "相關中文")

    def test_enrich_does_not_cache_title_only(self):
        """Cover gate: filling zh must not persist a payload without a real cover."""
        payload = {
            "ok": True,
            "code": "DOSD-008",
            "title": "gemini only",
            "title_zh": None,
            "cover": None,
            "related_by_title": [
                {"code": "DOSD-009", "title": "rel"},
            ],
        }
        with mock.patch.object(S, "resolve_chinese_title", return_value="中文"):
            out = S.enrich_offline_cache_hit(payload)
        self.assertEqual(out.get("title_zh"), "中文")
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))


class TestEntryId(unittest.TestCase):
    def test_same_entry_id(self):
        self.assertEqual(S._offline_cache_entry_id("NHDTC-99"), S._offline_cache_entry_id("NHDTC-099"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
