#!/usr/bin/env python3
"""Light unit tests for visual same_work enforcement and related caps."""
from __future__ import annotations

import os
import sys
import unittest

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
        # Simulate find_related_by_title final ordering logic with caps
        items = []
        for i in range(7):
            items.append({"code": f"AAA-{i+1:03d}", "line": "theme", "why": "片名相近"})
        for i in range(6):
            items.append({"code": f"BBB-{i+1:03d}", "line": "keyword", "why": "名稱關鍵字", "keyword_hits": i})
        for i in range(5):
            items.append({"code": f"CCC-{i+1:03d}", "line": "actress", "why": "同演員"})

        title_cap, keyword_cap, actress_cap = 5, 5, 3
        theme_items = [x for x in items if x["line"] == "theme"][:title_cap]
        keyword_items = [x for x in items if x["line"] == "keyword"][:keyword_cap]
        actress_items = [x for x in items if x["line"] == "actress"][:actress_cap]
        keyword_items.sort(key=lambda x: int(x.get("keyword_hits") or 0), reverse=True)
        ordered = theme_items + keyword_items + actress_items

        self.assertEqual(len(theme_items), 5)
        self.assertEqual(len(keyword_items), 5)
        self.assertEqual(len(actress_items), 3)
        self.assertEqual(ordered[0]["line"], "theme")
        self.assertEqual(ordered[5]["line"], "keyword")
        self.assertEqual(ordered[10]["line"], "actress")
        self.assertLessEqual(sum(1 for x in ordered if x["line"] == "actress"), 3)

    def test_theme_lexicon_has_tutor(self):
        self.assertIn("家庭教師", S._THEME_KEYWORD_LEXICON)

    def test_sibling_phrases_include_quoted_hook(self):
        title = "「今日も息子の家庭教師とセックスしています。」2人きりになったら10秒で挿入"
        phrases = S._title_sibling_phrases(title)
        blob = " ".join(phrases)
        self.assertTrue(
            any("家庭教師" in p for p in phrases),
            f"expected tutor hook in phrases, got {phrases[:8]}",
        )
        self.assertTrue(any("今日も息子" in p for p in phrases), blob)


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
