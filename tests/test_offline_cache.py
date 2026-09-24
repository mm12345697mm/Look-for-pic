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
        # Cache hits with no actress must not call the live catalog in these tests.
        self._avbase = mock.patch.object(S, "fetch_avbase_by_code", return_value=None)
        self._avbase.start()

    def tearDown(self):
        self._avbase.stop()
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

    def _keep_seed_related(self):
        def keep_seed(*args, **kwargs):
            return list(kwargs.get("seed") or [])

        return mock.patch.object(S, "find_related_by_title", side_effect=keep_seed)

    def test_enrich_fills_main_and_related_then_puts(self):
        S.offline_cache_put(self._stored_payload())
        hit = S.offline_cache_get(code="NHDTC-099")
        self.assertIsNotNone(hit)
        self.assertFalse(hit.get("title_zh"))
        self.assertFalse((hit.get("related_by_title") or [{}])[0].get("title_zh"))
        with self._keep_seed_related(), mock.patch.object(
            S, "resolve_chinese_title", side_effect=self._fake_resolve
        ):
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
        with self._keep_seed_related(), mock.patch.object(
            S, "resolve_chinese_title", side_effect=self._fake_resolve
        ):
            out = S.identify_code("NHDTC-099")
        self.assertTrue(out.get("from_offline_cache"))
        self.assertEqual(out.get("title_zh"), "中文主標")
        rel = out.get("related_by_title") or []
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0].get("title_zh"), "中文相關")

    def test_pipeline_manual_code_cache_hit_enriches(self):
        S.offline_cache_put(self._stored_payload())
        with self._keep_seed_related(), mock.patch.object(
            S, "resolve_chinese_title", side_effect=self._fake_resolve
        ):
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
        items = []
        for i in range(5):
            items.append(
                {
                    "code": f"THM-{i+1:03d}",
                    "title": "テーマ",
                    "title_zh": "主題",
                    "line": "theme",
                    "cover": f"https://example.com/t{i}.jpg",
                }
            )
        for i in range(5):
            items.append(
                {
                    "code": f"KEY-{i+1:03d}",
                    "title": "キーワード",
                    "title_zh": "關鍵字",
                    "line": "keyword",
                    "cover": f"https://example.com/k{i}.jpg",
                }
            )
        for i in range(3):
            items.append(
                {
                    "code": f"ACT-{i+1:03d}",
                    "title": "女優作",
                    "title_zh": "同演員",
                    "line": "actress",
                    "cover": f"https://example.com/a{i}.jpg",
                }
            )
        payload = self._stored_payload(title_zh="已有中文", related_by_title=items)
        S.offline_cache_put(payload)
        hit = S.offline_cache_get(code="NHDTC-099")
        with mock.patch.object(
            S, "resolve_chinese_title", side_effect=AssertionError("should not fetch zh")
        ), mock.patch.object(
            S, "find_related_by_title", side_effect=AssertionError("should not fetch related")
        ), mock.patch.object(
            S, "search_by_title", side_effect=AssertionError("should not search")
        ):
            out = S.enrich_offline_cache_hit(hit)
        self.assertEqual(out.get("title_zh"), "已有中文")
        t, k, a = S._related_bucket_counts(out.get("related_by_title") or [])
        self.assertEqual((t, k, a), (5, 5, 3))
        self.assertFalse(S._payload_needs_enrichment(out))

    def test_enrich_retries_when_flags_set_but_zh_missing(self):
        """Flags must not freeze incomplete related Chinese titles."""
        payload = self._stored_payload(title_zh="中文主標")
        payload["cache_backfilled"] = True
        payload["chinese_titles_attached"] = True
        self.assertTrue(S._payload_needs_title_zh(payload))
        with self._keep_seed_related(), mock.patch.object(
            S, "resolve_chinese_title", side_effect=self._fake_resolve
        ):
            out = S.enrich_offline_cache_hit(payload)
        self.assertEqual(out.get("title_zh"), "中文主標")
        self.assertEqual(out["related_by_title"][0].get("title_zh"), "中文相關")

    def test_enrich_keeps_under_cap_related_without_searching(self):
        """A short saved list is finished. Caps are maxima, not a reason to search."""
        payload = self._stored_payload(
            title_zh="中文主標",
            actress="誰か",
            related_by_title=[
                {
                    "code": "NHDTC-100",
                    "title": "関連作",
                    "title_zh": "中文相關",
                    "line": "theme",
                    "why": "片名相近",
                    "cover": "https://example.com/r.jpg",
                }
            ],
        )
        payload["cache_backfilled"] = True
        payload["chinese_titles_attached"] = True
        with mock.patch.object(
            S, "find_related_by_title", side_effect=AssertionError("saved related must not be re-searched")
        ), mock.patch.object(
            S, "resolve_chinese_title", side_effect=AssertionError("zh already present on coded rows")
        ):
            out = S.enrich_offline_cache_hit(payload)
        codes = [r["code"] for r in out.get("related_by_title") or []]
        self.assertEqual(codes, ["NHDTC-100"])

    def test_payload_needs_title_zh_only_for_coded(self):
        self.assertFalse(S._payload_needs_title_zh({"ok": True, "title": "no-code"}))
        self.assertTrue(
            S._payload_needs_title_zh({"ok": True, "code": "AAA-001", "title": "x"})
        )
        self.assertFalse(
            S._payload_needs_title_zh(
                {"ok": True, "code": "AAA-001", "title": "x", "title_zh": "中文"}
            )
        )

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
        with self._keep_seed_related(), mock.patch.object(S, "resolve_chinese_title", return_value="中文"):
            out = S.enrich_offline_cache_hit(payload)
        self.assertEqual(out.get("title_zh"), "中文")
        self.assertIsNone(S.offline_cache_get(code="DOSD-008"))


class TestIncrementalRelatedBackfill(unittest.TestCase):
    def test_cap_related_buckets_maxima_not_quotas(self):
        items = []
        for i in range(7):
            items.append({"code": f"AAA-{i+1:03d}", "line": "theme", "why": "片名相近"})
        for i in range(6):
            items.append(
                {
                    "code": f"BBB-{i+1:03d}",
                    "line": "keyword",
                    "why": "名稱關鍵字",
                    "keyword_hits": i,
                }
            )
        for i in range(5):
            items.append({"code": f"CCC-{i+1:03d}", "line": "actress", "why": "同演員"})
        ordered = S._cap_related_buckets(items)
        t, k, a = S._related_bucket_counts(ordered)
        self.assertEqual((t, k, a), (5, 5, 3))
        self.assertEqual(ordered[0]["line"], "theme")
        self.assertEqual(ordered[5]["line"], "keyword")
        self.assertEqual(ordered[10]["line"], "actress")
        self.assertLessEqual(len(ordered), 13)

    def test_seed_keeps_existing_and_fills_remaining_theme(self):
        title = "息子の家庭教師とセックスしています"
        seed = [
            {
                "code": f"AAA-{i:03d}",
                "title": title,
                "title_zh": f"中文{i}",
                "cover": f"https://example.com/{i}.jpg",
                "line": "theme",
                "why": "片名相近",
            }
            for i in range(1, 4)
        ]
        extra_cands = [
            {"code": "AAA-004", "title": title, "score": 0.95, "cover": "https://example.com/4.jpg"},
            {"code": "AAA-005", "title": title, "score": 0.94, "cover": "https://example.com/5.jpg"},
        ]
        hit = {
            "code": "AAA-004",
            "title": title,
            "score": 0.95,
            "cover": "https://example.com/4.jpg",
            "candidates": extra_cands,
        }

        def fake_enrich(c, why="片名相近"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "search_by_title", return_value=hit), mock.patch.object(
            S, "fetch_avbase_title_results", return_value=[]
        ), mock.patch.object(S, "_find_related_by_keywords", return_value=[]), mock.patch.object(
            S, "_find_related_by_actress", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            out = S.find_related_by_title(
                title,
                exclude_code="MAIN-001",
                seed=seed,
                fill_keyword=False,
                fill_actress=False,
                budget_sec=20.0,
            )
        codes = [x["code"] for x in out]
        self.assertEqual(codes[:3], ["AAA-001", "AAA-002", "AAA-003"])
        self.assertIn("AAA-004", codes)
        self.assertIn("AAA-005", codes)
        self.assertEqual(out[0].get("title_zh"), "中文1")
        self.assertEqual(out[0].get("cover"), "https://example.com/1.jpg")
        t, k, a = S._related_bucket_counts(out)
        self.assertEqual(t, 5)
        self.assertEqual(k, 0)
        self.assertEqual(a, 0)

    def test_full_buckets_skip_related_network(self):
        seed = []
        for i in range(5):
            seed.append({"code": f"THM-{i+1:03d}", "title": "t", "line": "theme", "why": "片名相近"})
        for i in range(5):
            seed.append({"code": f"KEY-{i+1:03d}", "title": "k", "line": "keyword", "why": "名稱關鍵字"})
        for i in range(3):
            seed.append({"code": f"ACT-{i+1:03d}", "title": "a", "line": "actress", "why": "同演員"})
        with mock.patch.object(
            S, "search_by_title", side_effect=AssertionError("theme bucket full")
        ), mock.patch.object(
            S, "fetch_avbase_title_results", side_effect=AssertionError("theme bucket full")
        ), mock.patch.object(
            S, "_find_related_by_keywords", side_effect=AssertionError("keyword bucket full")
        ), mock.patch.object(
            S, "_find_related_by_actress", side_effect=AssertionError("actress bucket full")
        ):
            out = S.find_related_by_title(
                "息子の家庭教師とセックスしています",
                exclude_code="MAIN-001",
                seed=seed,
                actress="誰か",
                budget_sec=20.0,
            )
        t, k, a = S._related_bucket_counts(out)
        self.assertEqual((t, k, a), (5, 5, 3))
        self.assertEqual(out[0]["code"], "THM-001")

    def test_stills_backfill_keeps_existing(self):
        payload = {
            "ok": True,
            "code": "NHDTC-099",
            "cid": "1nhdtc00099",
            "stills": ["https://example.com/keep.jpg"],
        }
        S._backfill_main_stills(payload)
        self.assertEqual(payload["stills"][0], "https://example.com/keep.jpg")
        self.assertGreaterEqual(len(payload["stills"]), 2)
        self.assertTrue(all("keep.jpg" in u or "1nhdtc00099" in u for u in payload["stills"]))

    def test_put_unions_stills_without_wipe(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            S._OFFLINE_CACHE_PATH = Path(tmp.name) / "offline-cache.json"
            S.offline_cache_put(
                {
                    "ok": True,
                    "code": "NHDTC-099",
                    "title": "t",
                    "cover": "https://example.com/c.jpg",
                    "stills": ["https://example.com/old.jpg"],
                }
            )
            S.offline_cache_put(
                {
                    "ok": True,
                    "code": "NHDTC-099",
                    "title": "t",
                    "cover": "https://example.com/c.jpg",
                    "stills": ["https://example.com/new.jpg"],
                }
            )
            hit = S.offline_cache_get(code="NHDTC-099")
            self.assertIn("https://example.com/old.jpg", hit["stills"])
            self.assertIn("https://example.com/new.jpg", hit["stills"])
        finally:
            S._OFFLINE_CACHE_PATH = None
            tmp.cleanup()

    def test_enrich_keeps_saved_related_membership(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            S._OFFLINE_CACHE_PATH = Path(tmp.name) / "offline-cache.json"
            seed_rel = [
                {
                    "code": "AAA-001",
                    "title": "keep me",
                    "title_zh": "留下",
                    "cover": "https://example.com/1.jpg",
                    "line": "theme",
                    "why": "片名相近",
                }
            ]
            S.offline_cache_put(
                {
                    "ok": True,
                    "code": "NHDTC-099",
                    "title": "息子の家庭教師とセックスしています",
                    "title_zh": "主中文",
                    "cover": "https://example.com/c.jpg",
                    "stills": ["https://example.com/s.jpg"],
                    "related_by_title": seed_rel,
                    "actress": "誰か",
                }
            )
            hit = S.offline_cache_get(code="NHDTC-099")
            with mock.patch.object(
                S, "find_related_by_title", side_effect=AssertionError("saved related must not be re-searched")
            ), mock.patch.object(
                S, "resolve_chinese_title", side_effect=AssertionError("zh already present")
            ):
                out = S.enrich_offline_cache_hit(hit)
            codes = [r["code"] for r in out["related_by_title"]]
            self.assertEqual(codes, ["AAA-001"])
            self.assertEqual(out["related_by_title"][0]["title_zh"], "留下")
            self.assertEqual(out["stills"][0], "https://example.com/s.jpg")
        finally:
            S._OFFLINE_CACHE_PATH = None
            tmp.cleanup()


class TestRelatedByTitleApi(unittest.TestCase):
    def _full_related(self, *, with_zh: bool):
        items = []
        for i in range(5):
            items.append(
                {
                    "code": f"THM-{i+1:03d}",
                    "title": "テーマ作品タイトル",
                    "title_zh": "主題中文" if with_zh else "",
                    "line": "theme",
                    "why": "片名相近",
                    "cover": f"https://example.com/t{i}.jpg",
                    "cid": f"thm{i+1:03d}",
                }
            )
        for i in range(5):
            items.append(
                {
                    "code": f"KEY-{i+1:03d}",
                    "title": "キーワード作品タイトル",
                    "title_zh": "關鍵字中文" if with_zh else "",
                    "line": "keyword",
                    "why": "關鍵字",
                    "cover": f"https://example.com/k{i}.jpg",
                    "cid": f"key{i+1:03d}",
                }
            )
        for i in range(3):
            items.append(
                {
                    "code": f"ACT-{i+1:03d}",
                    "title": "女優作タイトルです",
                    "title_zh": "同演員中文" if with_zh else "",
                    "line": "actress",
                    "why": "同演員",
                    "cover": f"https://example.com/a{i}.jpg",
                    "cid": f"act{i+1:03d}",
                }
            )
        return items

    def test_post_seed_at_cap_skips_search_and_fills_zh(self):
        seed = self._full_related(with_zh=False)
        resolved = []

        def fake_resolve(code, title_ja=None, existing_zh=None, user_title=None):
            resolved.append(str(code or ""))
            return "補中文"

        with mock.patch.object(
            S, "find_related_by_title", side_effect=AssertionError("buckets full — no search")
        ), mock.patch.object(S, "resolve_chinese_title", side_effect=fake_resolve):
            client = S.app.test_client()
            res = client.post(
                "/api/related-by-title",
                json={
                    "title": "テーマ作品タイトル",
                    "code": "MAIN-001",
                    "actress": "誰か",
                    "seed": seed,
                },
            )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        related = data.get("related_by_title") or []
        self.assertEqual(len(related), 13)
        self.assertTrue(all(str(r.get("title_zh") or "").strip() for r in related))
        self.assertTrue(resolved)


class TestEntryId(unittest.TestCase):
    def test_same_entry_id(self):
        self.assertEqual(S._offline_cache_entry_id("NHDTC-99"), S._offline_cache_entry_id("NHDTC-099"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
