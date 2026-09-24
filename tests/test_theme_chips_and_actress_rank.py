#!/usr/bin/env python3
"""Rich JP title chips, and same-actress ranking by keyword overlap then fame.

The swim-camp title is a regression shape (compound / role / setting themes
were dropped, leaving 巨乳 and 媚薬). It is not a hardcoded product code.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


SWIM = "巨乳水泳部員 媚薬漬けレ●プ合宿"
TRACK = "陸上部員を媚薬漬けにした合宿"
ACTRESS = "架空花子"


class TestRichTitleThemeChips(unittest.TestCase):
    def test_swim_camp_title_keeps_kyonyu_and_the_missing_themes(self):
        kws = S._extract_title_theme_keywords(SWIM)
        # Compound first, then 巨乳 in title order. It is not demoted off the list.
        self.assertEqual(kws, ["媚薬漬け", "巨乳", "水泳部員", "合宿", "媚薬"], kws)
        self.assertLess(kws.index("媚薬漬け"), kws.index("巨乳"))
        self.assertLess(kws.index("巨乳"), kws.index("水泳部員"))
        self.assertLess(kws.index("媚薬漬け"), kws.index("媚薬"))
        self.assertFalse(S._is_weak_theme_token("巨乳"))
        self.assertTrue(S._is_auto_theme_keyword("巨乳"))
        self.assertIn("巨乳", kws)
        for absent in ("スク水", "レ●プ", "レ○プ", "レイプ", "水泳部", "漬け", "BOD"):
            self.assertNotIn(absent, kws, kws)
        self.assertLessEqual(len(kws), 5)
        # Circle-censored spellings stay out even after OCR circle folding.
        folded = S._extract_title_theme_keywords(S.normalize_ocr_title(SWIM))
        self.assertEqual(folded, kws)
        self.assertFalse(any(S._is_censored_keyword(tok) for tok in folded), folded)

    def test_same_rules_on_a_different_club_title(self):
        kws = S._extract_title_theme_keywords(TRACK)
        self.assertEqual(kws, ["媚薬漬け", "陸上部員", "合宿", "媚薬"], kws)
        self.assertNotIn("水泳部員", kws)
        self.assertNotIn("スク水", kws)

    def test_club_without_member_suffix_and_particle_block(self):
        camp = S._extract_title_theme_keywords("水泳部の合宿")
        self.assertEqual(camp, ["水泳部", "合宿"], camp)
        parted = S._extract_title_theme_keywords("媚薬の漬けと巨乳")
        self.assertIn("媚薬", parted)
        self.assertIn("巨乳", parted)
        self.assertNotIn("媚薬漬け", parted)
        self.assertNotIn("漬け", parted)

    def test_body_generic_alone_is_kept(self):
        self.assertEqual(S._extract_title_theme_keywords("巨乳"), ["巨乳"])

    def test_edition_junk_still_stripped_beside_the_new_themes(self):
        kws = S._extract_title_theme_keywords("巨乳水泳部員の媚薬漬け合宿 (BOD)")
        self.assertIn("水泳部員", kws)
        self.assertIn("媚薬漬け", kws)
        self.assertIn("合宿", kws)
        self.assertNotIn("BOD", kws)
        self.assertTrue(all("bod" not in tok.casefold() for tok in kws), kws)

    def test_queries_lead_with_the_compound_and_keep_kyonyu(self):
        kws = S._extract_title_theme_keywords(SWIM)
        qs = S._keyword_search_queries(SWIM, kws)
        self.assertEqual(qs[:4], ["媚薬漬け", "巨乳", "水泳部員", "合宿"], qs)
        self.assertLessEqual(len(qs), 8)
        for absent in ("スク水", "レ●プ", "レ○プ", "レイプ"):
            self.assertNotIn(absent, qs, qs)
        self.assertTrue(S._keyword_token_ok("OL"))
        self.assertTrue(S._keyword_token_ok("合宿"))
        self.assertFalse(S._keyword_token_ok("レ●プ"))
        self.assertFalse(S._keyword_token_ok("レ○プ"))

    def test_shinjitai_and_traditional_drug_alias_match(self):
        self.assertIn(
            "媚薬",
            S._matched_theme_keywords("媚藥漬けの合宿", ["媚薬"]),
        )
        self.assertIn(
            "媚藥",
            S._matched_theme_keywords("媚薬漬けの合宿", ["媚藥"]),
        )


class TestSameActressKeywordThenFame(unittest.TestCase):
    def _rows(self):
        return [
            {
                "code": "CAMP-001",
                "title": "水泳部員の合宿",
                "actress": ACTRESS,
                "score": 0.2,
                "fame": 1,
            },
            {
                "code": "FAME-009",
                "title": "全く別の日常ドラマ",
                "actress": ACTRESS,
                "score": 0.99,
                "fame": 80,
            },
            {
                "code": "BODY-003",
                "title": "巨乳だけの作品",
                "actress": ACTRESS,
                "score": 0.8,
                "fame": 40,
            },
            {
                "code": "OTHR-001",
                "title": "水泳部員の合宿",
                "actress": "別人",
                "score": 1,
                "fame": 100,
            },
        ]

    def test_keyword_overlap_excludes_unrelated_even_if_famous(self):
        kws = S._extract_title_theme_keywords(SWIM)
        with mock.patch.object(S, "fetch_avbase_title_results", return_value=self._rows()):
            rows = S._find_related_by_actress(ACTRESS, keywords=kws, max_n=3)
        codes = [r["code"] for r in rows]
        self.assertEqual(codes, ["CAMP-001", "BODY-003"], codes)
        self.assertNotIn("FAME-009", codes)
        self.assertNotIn("OTHR-001", codes)
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(all(r.get("line") == "actress" for r in rows))

    def test_no_overlap_falls_back_to_fame_not_catalog_order(self):
        kws = S._extract_title_theme_keywords(SWIM)
        catalog = [
            {"code": "LOW-001", "title": "無関係A", "actress": ACTRESS, "score": 0.99, "fame": 1},
            {"code": "HIGH-002", "title": "無関係B", "actress": ACTRESS, "score": 0.1, "fame": 9},
            {"code": "MID-003", "title": "無関係C", "actress": ACTRESS, "score": 0.5, "fame": 4},
            {"code": "NO-004", "title": "水泳部の合宿", "actress": "別人", "score": 1, "fame": 50},
        ]
        with mock.patch.object(S, "fetch_avbase_title_results", return_value=catalog):
            rows = S._find_related_by_actress(ACTRESS, keywords=kws, max_n=3)
        self.assertEqual([r["code"] for r in rows], ["HIGH-002", "MID-003", "LOW-001"])

    def test_theme_less_bestof_loses_to_featured_works(self):
        """ハイパーベスト / 総集編 with no shared theme and no 劇照 is filler.

        The shape is a 2-disc hyper-best (blurry jacket, empty stills), not a
        hardcoded product code. Keyword overlap still wins when a best-of
        actually shares a theme.
        """
        kws = S._extract_title_theme_keywords(SWIM)
        self.assertFalse(S._is_compilation_title(SWIM))
        best_title = f"{ACTRESS} 2枚組ハイパーベスト4時間59分"
        omnibus = f"{ACTRESS} 総集編"
        self.assertTrue(S._is_compilation_title(best_title))
        self.assertTrue(S._is_compilation_title(omnibus))
        self.assertTrue(S._is_compilation_title("8時間ベスト"))
        self.assertFalse(S._is_compilation_title("巨乳水泳部員の合宿"))
        catalog = [
            {
                "code": "BEST-774",
                "title": best_title,
                "actress": ACTRESS,
                "score": 0.99,
                "fame": 100,
                "cover": "https://example.com/blurry.jpg",
                "stills": [],
            },
            {
                "code": "OMNI-001",
                "title": omnibus,
                "actress": ACTRESS,
                "score": 0.9,
                "fame": 90,
                "cover": "https://example.com/omni.jpg",
                "stills": [],
            },
            {
                "code": "FEAT-010",
                "title": "水泳部員の媚薬漬け合宿",
                "actress": ACTRESS,
                "score": 0.2,
                "fame": 3,
                "cid": "feat010",
                "cover": "https://example.com/feat.jpg",
            },
            {
                "code": "FEAT-011",
                "title": "別の巨乳物語",
                "actress": ACTRESS,
                "score": 0.4,
                "fame": 8,
                "cid": "feat011",
                "cover": "https://example.com/feat2.jpg",
            },
        ]
        with mock.patch.object(S, "fetch_avbase_title_results", return_value=catalog):
            rows = S._find_related_by_actress(ACTRESS, keywords=kws, max_n=3)
        codes = [r["code"] for r in rows]
        self.assertEqual(codes, ["FEAT-010", "FEAT-011"], codes)
        self.assertNotIn("BEST-774", codes)
        self.assertNotIn("OMNI-001", codes)
        self.assertLessEqual(len(rows), 3)

        # No theme overlap at all: notable featured works, not the best-of.
        plain = [
            {
                "code": "BEST-774",
                "title": best_title,
                "actress": ACTRESS,
                "score": 0.99,
                "fame": 100,
                "cover": "https://example.com/blurry.jpg",
                "stills": [],
            },
            {
                "code": "FEAT-002",
                "title": "話題の単体作品",
                "actress": ACTRESS,
                "score": 0.2,
                "fame": 6,
                "cid": "feat002",
                "cover": "https://example.com/feat.jpg",
                "stills": ["https://example.com/s1.jpg"],
            },
            {
                "code": "FEAT-003",
                "title": "もう一本の単体作品",
                "actress": ACTRESS,
                "score": 0.3,
                "fame": 4,
                "cid": "feat003",
                "cover": "https://example.com/feat3.jpg",
            },
        ]
        with mock.patch.object(S, "fetch_avbase_title_results", return_value=plain):
            fame_rows = S._find_related_by_actress(ACTRESS, keywords=kws, max_n=3)
        self.assertEqual([r["code"] for r in fame_rows], ["FEAT-002", "FEAT-003"])

        # A best-of that actually shares the theme still beats an unrelated feature.
        themed = [
            {
                "code": "BEST-100",
                "title": "水泳部員ハイパーベスト",
                "actress": ACTRESS,
                "score": 0.2,
                "fame": 1,
                "cover": "https://example.com/b.jpg",
                "stills": [],
            },
            {
                "code": "FEAT-900",
                "title": "無関係な単体作",
                "actress": ACTRESS,
                "score": 0.99,
                "fame": 80,
                "cid": "feat900",
                "cover": "https://example.com/f.jpg",
            },
        ]
        with mock.patch.object(S, "fetch_avbase_title_results", return_value=themed):
            themed_rows = S._find_related_by_actress(ACTRESS, keywords=kws, max_n=3)
        self.assertEqual([r["code"] for r in themed_rows], ["BEST-100"])

    def test_edition_count_is_fame_when_no_popularity_field(self):
        self.assertGreater(
            S._candidate_fame({"product_count": 4, "score": 0.1}),
            S._candidate_fame({"product_count": 1, "score": 0.99}),
        )
        self.assertGreater(
            S._avbase_work_fame({"review_count": 12}, [{"product_id": "a"}]),
            S._avbase_work_fame({}, [{"product_id": "a"}, {"product_id": "b"}, {"product_id": "c"}]),
        )

    def test_related_by_title_passes_theme_keywords_into_actress_bucket(self):
        captured: dict = {}

        def fake_actress(name, **kwargs):
            captured["name"] = name
            captured["keywords"] = list(kwargs.get("keywords") or [])
            return []

        with mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
            S, "fetch_avbase_title_results", return_value=[]
        ), mock.patch.object(S, "_find_related_by_keywords", return_value=[]), mock.patch.object(
            S, "_find_related_by_actress", side_effect=fake_actress
        ):
            S.find_related_by_title(SWIM, actress=ACTRESS, budget_sec=5)
        self.assertEqual(captured.get("name"), ACTRESS)
        kws = captured.get("keywords") or []
        for tok in ("媚薬漬け", "水泳部員", "合宿", "巨乳"):
            self.assertIn(tok, kws, kws)


if __name__ == "__main__":
    unittest.main(verbosity=2)
