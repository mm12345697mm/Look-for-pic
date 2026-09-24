#!/usr/bin/env python3
"""Keyword chips must cover a title's real themes, not one or two leftovers.

ROYD-343 is the screenshot case: the Japanese title is an almost-private
cinema, a slender big-breasted blonde gal, and 乳首. The old whitelist kept
only 巨乳 and 乳首. Catalog genres add the rest of the real tags and drop
picture-quality / distribution chrome. OCR scraps and edition marks stay out.
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


ROYD_TITLE = (
    "ほぼ貸切の映画館で…隣に座る細身巨乳な金髪ギャルに乳首とチ○ポを弄られ続けて"
    "射精にしか集中できなかったボク…。"
)
# Title order. 射精 is a real act word and stays behind the look/setting nouns.
ROYD_TITLE_CHIPS = ["映画館", "細身", "巨乳", "金髪", "ギャル", "乳首", "射精"]
# Javbus /ja/ genres for this code, after format tags are removed.
ROYD_CATALOG_GENRES = [
    "痴女",
    "中出し",
    "ハイビジョン",
    "巨乳",
    "単体作品",
    "独占配信",
    "ギャル",
    "手コキ",
]
ROYD_WITH_CATALOG = [
    "映画館",
    "細身",
    "巨乳",
    "金髪",
    "ギャル",
    "乳首",
    "手コキ",
    "痴女",
    "射精",
    "中出し",
]


class TestRoyd343KeywordSufficiency(unittest.TestCase):
    def test_title_chips_cover_cinema_look_and_body(self):
        kws = S._extract_title_theme_keywords(ROYD_TITLE, actress="百永さりな")
        self.assertEqual(kws, ROYD_TITLE_CHIPS, kws)
        for tok in ("映画館", "金髪", "ギャル", "細身", "巨乳", "乳首"):
            self.assertIn(tok, kws)
        self.assertLess(kws.index("巨乳"), kws.index("乳首"))
        self.assertLess(kws.index("巨乳"), kws.index("射精"))
        for absent in ("ボク", "チ○ポ", "play", "cinema", "ハイビジョン", "単体作品"):
            self.assertNotIn(absent, kws)
        self.assertNotIn("ノーブラ誘惑", kws)
        qs = S._keyword_search_queries(ROYD_TITLE, kws)
        self.assertIn("巨乳", qs)
        self.assertIn("映画館", qs)
        self.assertIn("ギャル", qs)
        self.assertIn("金髪", qs)
        self.assertIn("細身", qs)
        self.assertLessEqual(len(qs), 8)
        self.assertTrue(all(not S._is_format_genre(q) for q in qs), qs)

    def test_catalog_genres_add_real_tags_and_drop_format(self):
        payload = S._recompute_theme_keywords(
            {
                "title": ROYD_TITLE,
                "actress": "百永さりな",
                "genres": list(ROYD_CATALOG_GENRES),
            }
        )
        self.assertEqual(payload["theme_keywords"], ROYD_WITH_CATALOG, payload["theme_keywords"])
        for tok in ("痴女", "手コキ", "中出し", "巨乳", "ギャル", "映画館"):
            self.assertIn(tok, payload["theme_keywords"])
        for junk in ("ハイビジョン", "単体作品", "独占配信", "百永さりな"):
            self.assertNotIn(junk, payload["theme_keywords"])
        self.assertIn("巨乳", payload["keyword_queries"])

    def test_related_package_gets_its_own_title_chips(self):
        stamped = S._stamp_listed_work_keywords(
            {
                "ok": True,
                "title": "別の本題",
                "theme_keywords": ["別"],
                "related_by_title": [
                    {
                        "code": "ROYD-343",
                        "title": ROYD_TITLE,
                        "actress": "百永さりな",
                        "line": "keyword",
                        "genres": list(ROYD_CATALOG_GENRES),
                    }
                ],
            }
        )
        related = stamped["related_by_title"][0]
        self.assertEqual(related["theme_keywords"], ROYD_WITH_CATALOG, related["theme_keywords"])
        self.assertIn("映画館", related["keyword_queries"])

    def test_kyonyu_survives_a_long_token_list(self):
        title = "映画館ホテル旅館教室学園学校病院風呂浴室車内金髪ギャル細身巨乳"
        kws = S._extract_title_theme_keywords(title)
        self.assertLessEqual(len(kws), 10, kws)
        self.assertIn("巨乳", kws, kws)
        self.assertGreater(len(kws), 2)

    def test_junk_ocr_does_not_flood_chips(self):
        junk = (
            "をな属人金欲にい ff\n短い 第2巻 (BOD) ハイビジョン 独占配信 "
            "単体作品 VOL.2 あいうえおかきくけこ !! l1I| マーメイド"
        )
        kws = S._extract_title_theme_keywords(junk)
        self.assertLessEqual(len(kws), 2, kws)
        blob = " ".join(kws)
        for bad in (
            "BOD",
            "VOL",
            "ハイビジョン",
            "独占配信",
            "単体作品",
            "をな属人金欲",
            "メイド",
            "play",
        ):
            self.assertNotIn(bad, blob, kws)
        mixed = S._extract_title_theme_keywords(
            "巨乳をな属人金欲乳首(BOD)ハイビジョン独占配信"
        )
        self.assertIn("巨乳", mixed)
        self.assertIn("乳首", mixed)
        self.assertLessEqual(len(mixed), 3, mixed)
        self.assertNotIn("ハイビジョン", mixed)
        self.assertNotIn("独占配信", mixed)
        self.assertFalse(any("をな属" in tok for tok in mixed), mixed)

    def test_katakana_boundary_keeps_real_compounds(self):
        oil = S._extract_title_theme_keywords("媚薬オイル")
        self.assertIn("媚薬", oil)
        self.assertIn("オイル", oil)
        self.assertNotIn("メイド", S._extract_title_theme_keywords("マーメイドの休日"))
        hotel = S._extract_title_theme_keywords("ラブホテルの巨乳")
        self.assertIn("ラブホテル", hotel)
        self.assertIn("巨乳", hotel)
        self.assertNotIn("ホテル", hotel)
        self.assertEqual(
            S._extract_title_theme_keywords("女子校生の制服"),
            ["女子校生", "制服"],
        )

    def test_nobra_split_and_kyonyu_priority_unchanged(self):
        title = "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク"
        kws = S._extract_title_theme_keywords(title)
        self.assertNotIn("ノーブラ誘惑", kws)
        self.assertEqual(kws[0], "ノーブラ", kws)
        self.assertEqual(kws[1], "巨乳", kws)
        self.assertIn("誘惑", kws)
        self.assertLess(kws.index("巨乳"), kws.index("誘惑"))


class TestJavbusGenreParse(unittest.TestCase):
    HTML = """
    <html><h3>ROYD-343 ほぼ貸切の映画館で…隣に座る細身巨乳な金髪ギャルに乳首とチ○ポを弄られ続けて射精にしか集中できなかったボク…。 百永さりな</h3>
    <p>ジャンル:</p>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="10">
    <a href="https://www.javbus.com/ja/genre/10">痴女</a></label></span>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="4o">
    <a href="https://www.javbus.com/ja/genre/4o">ハイビジョン</a></label></span>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="e">
    <a href="https://www.javbus.com/ja/genre/e">巨乳</a></label></span>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="f">
    <a href="https://www.javbus.com/ja/genre/f">単体作品</a></label></span>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="p">
    <a href="https://www.javbus.com/ja/genre/p">ギャル</a></label></span>
    <span class="genre"><label><input type="checkbox" name="gr_sel" value="x">
    <a href="https://www.javbus.com/ja/genre/x">手コキ</a></label></span>
    <a class="avatar-box" title="百永さりな"><img title="百永さりな"></a>
    </html>
    """

    def test_parse_drops_format_labels(self):
        genres = S._parse_javbus_genres(self.HTML)
        self.assertEqual(genres, ["痴女", "巨乳", "ギャル", "手コキ"], genres)

    def test_fetch_prefers_japanese_page(self):
        S._JAVBUS_SEARCH_MEMO.clear()
        seen: list[str] = []

        def fake_get(url, **kwargs):
            seen.append(str(url))

            class Resp:
                status_code = 200
                text = TestJavbusGenreParse.HTML

            return Resp()

        with mock.patch.object(S.requests, "get", side_effect=fake_get):
            row = S._fetch_javbus_by_code("ROYD-343")
        self.assertTrue(seen, seen)
        self.assertIn("/ja/ROYD-343", seen[0])
        self.assertIsNotNone(row)
        self.assertIn("映画館", row["title"])
        self.assertEqual(row["actress"], "百永さりな")
        self.assertEqual(row["genres"], ["痴女", "巨乳", "ギャル", "手コキ"])
        chips = S._recompute_theme_keywords(
            {"title": row["title"], "actress": row["actress"], "genres": row["genres"]}
        )
        for tok in ("映画館", "細身", "巨乳", "金髪", "ギャル", "乳首", "痴女", "手コキ"):
            self.assertIn(tok, chips["theme_keywords"], chips["theme_keywords"])
        self.assertNotIn("ハイビジョン", chips["theme_keywords"])


if __name__ == "__main__":
    unittest.main()
