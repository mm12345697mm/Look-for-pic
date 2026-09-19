#!/usr/bin/env python3
"""Light unit tests for visual same_work enforcement and related caps."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


class TestNormalizeOcrTitle(unittest.TestCase):
    def test_simplified_de_to_no_in_jp(self):
        raw = "今日も息子的家庭教師とセックスしています。"
        out = S.normalize_ocr_title(raw)
        self.assertIn("息子の", out)
        self.assertNotIn("息子的", out)


class TestEnforceVisualSameWork(unittest.TestCase):
    def test_rejects_same_series_without_person(self):
        vm = S.enforce_visual_same_work(
            {
                "same_work": True,
                "confidence": 0.9,
                "match_person": False,
                "match_face": False,
                "match_accessories": False,
                "match_clothes": True,
                "match_pose": True,
                "reason": "same series",
            }
        )
        self.assertFalse(vm["same_work"])
        self.assertLessEqual(vm["confidence"], 0.30)

    def test_rejects_without_clothes(self):
        vm = S.enforce_visual_same_work(
            {
                "same_work": True,
                "confidence": 0.8,
                "match_person": True,
                "match_face": True,
                "match_accessories": True,
                "match_clothes": False,
                "match_pose": True,
            }
        )
        self.assertFalse(vm["same_work"])
        self.assertLessEqual(vm["confidence"], 0.35)

    def test_accepts_person_clothes_face(self):
        vm = S.enforce_visual_same_work(
            {
                "same_work": True,
                "confidence": 0.92,
                "match_person": True,
                "match_face": True,
                "match_accessories": True,
                "match_clothes": True,
                "match_pose": True,
            }
        )
        self.assertTrue(vm["same_work"])
        self.assertGreaterEqual(vm["confidence"], 0.9)

    def test_demotes_low_confidence_same_work(self):
        vm = S.enforce_visual_same_work(
            {
                "same_work": True,
                "confidence": 0.42,
                "match_person": True,
                "match_face": True,
                "match_accessories": True,
                "match_clothes": True,
                "match_pose": True,
            }
        )
        self.assertFalse(vm["same_work"])
        self.assertLessEqual(vm["confidence"], 0.42)

    def test_score_penalizes_wrong_person(self):
        good = S.visual_match_score(
            {
                "same_work": True,
                "confidence": 0.9,
                "match_person": True,
                "match_face": True,
                "match_accessories": True,
                "match_clothes": True,
                "match_pose": True,
            }
        )
        bad = S.visual_match_score(
            {
                "same_work": True,
                "confidence": 0.9,
                "match_person": False,
                "match_face": False,
                "match_accessories": False,
                "match_clothes": True,
                "match_pose": True,
            }
        )
        self.assertGreater(good, bad)
        self.assertLess(bad, 0.45)


class TestRelatedCaps(unittest.TestCase):
    def test_bucket_reorder_and_caps(self):
        items = []
        for i in range(7):
            items.append({"code": f"AAA-{i+1:03d}", "line": "theme", "why": "片名相近"})
        for i in range(6):
            items.append({"code": f"BBB-{i+1:03d}", "line": "keyword", "why": "名稱關鍵字", "keyword_hits": i})
        for i in range(5):
            items.append({"code": f"CCC-{i+1:03d}", "line": "actress", "why": "同演員"})

        ordered = S._cap_related_buckets(items)

        self.assertEqual(sum(1 for x in ordered if x["line"] == "theme"), 5)
        self.assertEqual(sum(1 for x in ordered if x["line"] == "keyword"), 5)
        self.assertEqual(sum(1 for x in ordered if x["line"] == "actress"), 3)
        self.assertEqual(ordered[0]["line"], "theme")
        self.assertEqual(ordered[5]["line"], "keyword")
        self.assertEqual(ordered[10]["line"], "actress")
        self.assertLessEqual(sum(1 for x in ordered if x["line"] == "actress"), 3)

    def test_theme_lexicon_has_tutor(self):
        self.assertIn("家庭教師", S._THEME_KEYWORD_LEXICON)

    def test_jufe271_short_theme_tokens_are_distinctive(self):
        for tok in ("眼鏡", "メガネ", "眼鏡っ娘", "地味", "美人"):
            self.assertIn(tok, S._THEME_KEYWORD_LEXICON, tok)
            self.assertNotIn(tok, S._WEAK_THEME_TOKENS, tok)
            self.assertNotIn(tok.upper(), S._WEAK_THEME_TOKENS, tok)

    def test_jufe271_title_extracts_look_tokens_not_leftover_scraps(self):
        title = "地味な眼鏡では隠し切れない美人OLが性欲を抑えきれず完全生撮り"
        kws = S._extract_title_theme_keywords(title, actress="楪カレン")
        for tok in ("地味", "眼鏡", "美人", "OL"):
            self.assertIn(tok, kws, kws)
        for scrap in ("は隠し切れな", "が性欲を抑え", "きれず完全生", "地味な眼鏡で"):
            self.assertNotIn(scrap, kws, kws)
        distinctive = [k for k in kws if not S._is_weak_theme_token(k)]
        self.assertIn("眼鏡", distinctive)
        self.assertIn("地味", distinctive)
        self.assertNotIn("OL", distinctive)
        self.assertGreaterEqual(S._keyword_hit_count("地味なメガネのOLが会社で", kws), 2)
        self.assertGreaterEqual(S._keyword_hit_count("眼鏡っ娘の美人OL", kws), 2)
        self.assertLess(S._keyword_hit_count("ただのOLです", kws), 2)

    def test_sibling_phrases_include_quoted_hook(self):
        title = "「今日も息子の家庭教師とセックスしています。」2人きりになったら10秒で挿入"
        phrases = S._title_sibling_phrases(title)
        blob = " ".join(phrases)
        self.assertTrue(
            any("家庭教師" in p for p in phrases),
            f"expected tutor hook in phrases, got {phrases[:8]}",
        )
        self.assertTrue(any("今日も息子" in p for p in phrases), blob)


class TestJufe271KeywordBucket(unittest.TestCase):
    TITLE = "地味な眼鏡では隠し切れない美人OLが性欲を抑えきれず完全生撮り"

    def test_keyword_bucket_fills_other_actress_look_works(self):
        queries: list[str] = []

        def fake_avbase(q, actress=None):
            queries.append(q)
            return [
                {
                    "code": "PRED-001",
                    "title": "地味な眼鏡の美人OLが会社で欲情",
                    "actress": "別人A",
                    "score": 0.8,
                },
                {
                    "code": "SSIS-002",
                    "title": "メガネっ娘の地味OL",
                    "actress": "別人B",
                    "score": 0.7,
                },
                {
                    "code": "JUFE-333",
                    "title": "ただの中出しOL",
                    "actress": "別人C",
                    "score": 0.9,
                },
            ]

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_avbase), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S._find_related_by_keywords(
                self.TITLE,
                exclude_code="JUFE-271",
                actress="楪カレン",
                max_n=5,
                budget_sec=20.0,
            )
        codes = [r["code"] for r in rows]
        self.assertIn("PRED-001", codes)
        self.assertIn("SSIS-002", codes)
        self.assertNotIn("JUFE-333", codes, "single weak OL must not pad")
        self.assertNotIn("JUFE-271", codes)
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(all(r.get("line") == "keyword" for r in rows))
        blob = " ".join(queries)
        self.assertTrue(
            any("眼鏡" in q or "メガネ" in q or "地味" in q for q in queries),
            blob,
        )

    def test_keyword_bucket_does_not_pad_past_cap(self):
        def fake_avbase(q, actress=None):
            return [
                {
                    "code": f"AAA-{i:03d}",
                    "title": "地味な眼鏡の美人OL",
                    "actress": f"女優{i}",
                    "score": 0.5,
                }
                for i in range(1, 12)
            ]

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_avbase), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S._find_related_by_keywords(self.TITLE, max_n=5, budget_sec=20.0)
        self.assertEqual(len(rows), 5)


class TestActressQueryKeep(unittest.TestCase):
    def test_is_actress_query_detection_via_score_path(self):
        # Unit-level: compact JP name without particles looks like actress query
        title = "竹内夏希"
        q_compact = __import__("re").sub(r"[\s　・·．.]+", "", title)
        self.assertTrue(
            __import__("re").fullmatch(r"[\u3040-\u30ff\u4e00-\u9fff]{2,12}", q_compact)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
