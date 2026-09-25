#!/usr/bin/env python3
"""Same 品番 in one batch keeps one Chinese title. Timers are display-only."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


class BatchTitleZhTests(unittest.TestCase):
    def tearDown(self):
        while getattr(S._job_zh_state(), "depth", 0):
            S._job_zh_end()

    def test_harmonize_copies_same_code_without_fetch(self):
        payload = {
            "code": "MIDA-616",
            "title": "彼女の妹",
            "title_zh": "女友妹妹的誘惑",
            "results": [
                {"code": "MIDA-616", "title": "彼女の妹", "title_zh": "女友妹妹的誘惑"},
                {"code": "mida616", "title": "彼女の妹"},
            ],
            "related_by_title": [
                {"code": "MIDA-616", "title": "彼女の妹", "line": "theme"},
                {"code": "SONE-387", "title": "別作品", "line": "keyword"},
            ],
        }

        def boom(*_a, **_k):
            raise AssertionError("must not scrape")

        with mock.patch.object(S, "http_get", side_effect=boom):
            S.harmonize_batch_title_zh(payload)
        self.assertEqual(payload["results"][1]["title_zh"], "女友妹妹的誘惑")
        self.assertEqual(payload["related_by_title"][0]["title_zh"], "女友妹妹的誘惑")
        self.assertFalse(payload["related_by_title"][1].get("title_zh"))

    def test_nested_job_memo_survives_slot_end(self):
        S._job_zh_begin()
        try:
            S._job_zh_begin()
            S._remember_title_zh("MIDA-616", "女友妹妹的誘惑", "彼女の妹")
            S._job_zh_end()
            self.assertEqual(S._memo_title_zh("MIDA-616"), "女友妹妹的誘惑")
            payload = {
                "results": [
                    {"code": "MIDA-616", "title": "彼女の妹"},
                    {"code": "MIDA-616", "title": "彼女の妹"},
                ]
            }
            with mock.patch.object(S, "http_get", side_effect=AssertionError("fetch")):
                S.harmonize_batch_title_zh(payload)
            self.assertEqual(payload["results"][0]["title_zh"], "女友妹妹的誘惑")
            self.assertEqual(payload["results"][1]["title_zh"], "女友妹妹的誘惑")
        finally:
            S._job_zh_end()
        self.assertIsNone(S._memo_title_zh("MIDA-616"))

    def test_enrich_copies_donor_title_zh_onto_duplicate_code(self):
        payload = {
            "code": "ROYD-100",
            "title": "映画館",
            "related_by_title": [
                {"code": "ROYD-100", "title": "映画館", "line": "theme", "why": "片名相近"},
                {"code": "ROYD-100", "title": "映画館", "line": "keyword", "why": "關鍵字"},
            ],
        }

        def fake(item):
            if str(item.get("title_zh") or "").strip():
                return False
            item["title_zh"] = "電影院中文"
            return True

        with mock.patch.object(S, "enrich_coded_work_from_public_catalog", side_effect=fake):
            S.enrich_related_public_catalog(payload, budget_sec=30)
        self.assertEqual(payload.get("title_zh"), "電影院中文")
        kept = [r for r in payload["related_by_title"] if r.get("code") == "ROYD-100"]
        self.assertGreaterEqual(len(kept), 1)
        for row in kept:
            self.assertEqual(row.get("title_zh"), "電影院中文")

    def test_short_sourced_zh_is_shared_without_inventing(self):
        payload = {
            "code": "REAL-852",
            "title": "巨乳水泳部員",
            "title_zh": "巨乳泳社",
            "results": [
                {"code": "REAL-852", "title": "巨乳水泳部員", "title_zh": "巨乳泳社"},
                {"code": "REAL-852", "title": "巨乳水泳部員"},
                {"code": "DAS-034", "title": "別作品"},
            ],
        }
        with mock.patch.object(S, "http_get", side_effect=AssertionError("fetch")):
            S.harmonize_batch_title_zh(payload)
        self.assertEqual(payload["results"][1]["title_zh"], "巨乳泳社")
        self.assertFalse(payload["results"][2].get("title_zh"))
        self.assertIsNone(S._keep_stored_title_zh("巨乳水泳部員", title_ja="巨乳水泳部員", code="REAL-852"))
        self.assertIsNone(S._keep_stored_title_zh("彼女の妹", title_ja="別の題"))

    def test_missav_cn_title_without_particle_is_kept(self):
        html = (
            "<html><head><meta property=\"og:title\" "
            "content=\"REAL-852 巨乳泳社 - MissAV\"></head></html>"
        )

        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "missav.ai/cn/real-852" in url:
                return html
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("REAL-852", title_ja="巨乳水泳部員")
        self.assertEqual(meta["title_zh"], "巨乳泳社")

    def test_empty_live_fetch_reuses_stored_or_cached_zh(self):
        with mock.patch.object(S, "fetch_public_zh_catalog", return_value={"title_zh": None}):
            with mock.patch.object(S, "offline_cache_get", return_value=None):
                kept = S.resolve_chinese_title(
                    "REAL-852",
                    title_ja="巨乳水泳部員",
                    existing_zh="巨乳泳社",
                )
                empty = S.resolve_chinese_title("DAS-034", title_ja="日文題", existing_zh=None)
        self.assertEqual(kept, "巨乳泳社")
        self.assertFalse(empty)
        payload = {
            "code": "DAS-034",
            "title": "日文題",
            "results": [{"code": "DAS-034", "title": "日文題"}],
        }
        with mock.patch.object(
            S,
            "offline_cache_get",
            return_value={"title": "日文題", "title_zh": "巨乳集訓"},
        ):
            with mock.patch.object(S, "http_get", side_effect=AssertionError("fetch")):
                S.harmonize_batch_title_zh(payload, use_cache=True)
        self.assertEqual(payload["title_zh"], "巨乳集訓")
        self.assertEqual(payload["results"][0]["title_zh"], "巨乳集訓")

    def test_progress_event_carries_monotonic_mark(self):
        seen = []
        S._progress(seen.append, "search", "active", "搜尋第 1/2 張…", 0.55, phase="目錄查詢")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0].get("t_ms"), int)
        self.assertGreater(seen[0]["t_ms"], 0)
        self.assertNotIn("時間不夠", seen[0]["detail"])


if __name__ == "__main__":
    unittest.main()
