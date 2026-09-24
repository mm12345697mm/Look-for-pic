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
# MissAV /cn/ tags beyond the javbus list, plus the ROYAL cinema series.
# スレンダー / 淫語 are real page labels. The ten-chip cap keeps them and
# drops weak 射精 / 中出し. Format chrome and the maker (ROYAL) stay out.
ROYD_SERIES = "平日昼間の映画館で…（ROYAL）"
ROYD_WITH_MISSAV = [
    "映画館",
    "細身",
    "巨乳",
    "金髪",
    "ギャル",
    "乳首",
    "スレンダー",
    "手コキ",
    "痴女",
    "淫語",
]
ROYD_MISSAV_HTML = """
<html><head>
<meta property="og:title" content="ROYD-343 幾乎包場的電影院 - MissAV">
</head><body>
<a href="https://missav.ai/cn/actresses/momoe-sarina">百永紗里奈</a>
<a href="https://missav.ai/cn/genres/slut">蕩婦</a>
<a href="https://missav.ai/cn/genres/creampie">中出</a>
<a href="https://missav.ai/cn/genres/big-tits">巨乳</a>
<a href="https://missav.ai/cn/genres/gal">辣妹</a>
<a href="https://missav.ai/cn/genres/handjob">打手槍</a>
<a href="https://missav.ai/cn/genres/blonde">金髮</a>
<a href="https://missav.ai/cn/genres/slender">苗條</a>
<a href="https://missav.ai/cn/genres/dirty-talk">淫語</a>
<a href="https://missav.ai/cn/genres/cinema">電影院</a>
<a href="https://missav.ai/cn/genres/hd">高清</a>
<a href="https://missav.ai/cn/genres/solo">單體作品</a>
<a href="https://missav.ai/cn/genres/exclusive">獨家</a>
<a href="https://missav.ai/cn/makers/royal">ROYAL</a>
<a href="https://missav.ai/cn/series/heijitsu-hiruma-eigakan">平日昼間の映画館で…（ROYAL）</a>
</body></html>
"""
ROYD_JABLE_HTML = """
<html><body>
<a href="https://jable.tv/models/momoe-sarina/"><span>百永さりな</span></a>
<a href="https://jable.tv/categories/big-tits/" class="cat">巨乳</a>
<a href="https://jable.tv/tags/movie-theater/">電影院</a>
<a href="https://jable.tv/tags/dirty-talk/">淫語</a>
<a href="https://jable.tv/categories/hd/">高清</a>
</body></html>
"""


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


def _cinema_catalog() -> list[dict]:
    """High-score non-cinema hits, plus several cinema-series siblings."""
    spec = (
        ("BODY-001", "金髪ギャルの巨乳痴女", 9),
        ("BODY-002", "巨乳ギャルの乳首", 8),
        ("BODY-003", "細身巨乳の手コキ", 7),
        ("BODY-004", "ギャル巨乳痴女", 6),
        ("BODY-005", "金髪巨乳のギャル", 5),
        ("ROYD-100", "平日昼間の映画館で隣の細身巨乳", 2),
        ("ROYD-200", "平日昼間の映画館で巨乳", 1),
        ("ROYD-300", "平日昼間の映画館でまた", 1),
        ("ROYD-400", "平日昼間の映画館で三人", 1),
    )
    out = []
    for code, title, score in spec:
        slug = code.lower()
        out.append(
            {
                "code": code,
                "title": title,
                "score": score,
                "cover": f"https://pics.dmm.co.jp/digital/video/{slug}/{slug}pl.jpg",
            }
        )
    return out


class TestMissavTagsAndCinemaSeries(unittest.TestCase):
    """MissAV / Jable tags and the cinema series, beyond title + javbus."""

    def test_page_tags_and_series_are_parsed(self):
        genres, series = S._parse_public_catalog_tags(ROYD_MISSAV_HTML)
        self.assertEqual(series, ROYD_SERIES)
        for tok in ("痴女", "中出し", "巨乳", "ギャル", "手コキ", "金髪", "スレンダー", "淫語", "映画館"):
            self.assertIn(tok, genres, genres)
        for junk in ("高清", "單體作品", "獨家", "ROYAL", "百永紗里奈", "play", "cinema"):
            self.assertNotIn(junk, genres, genres)
        self.assertEqual(S._series_theme_tokens(series), ["映画館"])
        self.assertEqual(S._series_search_phrase(series), "平日昼間の映画館")
        jable_genres, jable_series = S._parse_public_catalog_tags(ROYD_JABLE_HTML)
        self.assertIsNone(jable_series)
        self.assertEqual(jable_genres, ["巨乳", "映画館", "淫語"], jable_genres)

    def test_fetch_folds_tags_into_the_same_zh_catalog_call(self):
        def fake_get(url, timeout=8.0, headers=None):
            if url == "https://missav.ai/cn/royd-343":
                return ROYD_MISSAV_HTML
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("ROYD-343", actress_ja="百永さりな", title_ja=ROYD_TITLE)
        self.assertIn("スレンダー", meta["genres"], meta)
        self.assertIn("淫語", meta["genres"])
        self.assertIn("映画館", meta["genres"])
        self.assertNotIn("高清", meta["genres"])
        self.assertNotIn("ROYAL", meta["genres"])
        self.assertEqual(meta["series"], ROYD_SERIES)

    def test_three_chip_layers_for_royd_343(self):
        title_only = S._extract_title_theme_keywords(ROYD_TITLE, actress="百永さりな")
        self.assertEqual(title_only, ROYD_TITLE_CHIPS, title_only)
        with_javbus = S._recompute_theme_keywords(
            {
                "title": ROYD_TITLE,
                "actress": "百永さりな",
                "genres": list(ROYD_CATALOG_GENRES),
            }
        )
        self.assertEqual(with_javbus["theme_keywords"], ROYD_WITH_CATALOG, with_javbus["theme_keywords"])
        genres, series = S._parse_public_catalog_tags(ROYD_MISSAV_HTML)
        merged = list(ROYD_CATALOG_GENRES)
        for tok in genres:
            if tok not in merged:
                merged.append(tok)
        with_missav = S._recompute_theme_keywords(
            {
                "title": ROYD_TITLE,
                "actress": "百永さりな",
                "genres": merged,
                "series": series,
            }
        )
        self.assertEqual(with_missav["theme_keywords"], ROYD_WITH_MISSAV, with_missav["theme_keywords"])
        self.assertLessEqual(len(with_missav["theme_keywords"]), 10)
        self.assertIn("巨乳", with_missav["theme_keywords"])
        self.assertLess(
            with_missav["theme_keywords"].index("巨乳"),
            with_missav["theme_keywords"].index("乳首"),
        )
        for tok in ("映画館", "スレンダー", "淫語", "手コキ", "痴女"):
            self.assertIn(tok, with_missav["theme_keywords"])
        for junk in ("ハイビジョン", "単体作品", "独占配信", "ROYAL", "play", "cinema", "百永さりな"):
            self.assertNotIn(junk, with_missav["theme_keywords"])
        self.assertNotIn("ノーブラ誘惑", with_missav["theme_keywords"])
        qs = with_missav["keyword_queries"]
        self.assertIn("映画館", qs, qs)
        self.assertIn("平日昼間の映画館", qs, qs)
        self.assertIn("巨乳", qs)
        self.assertLessEqual(len(qs), 8)
        self.assertTrue(all(not S._is_format_genre(q) for q in qs), qs)

    def test_series_token_is_a_chip_when_the_title_omits_it(self):
        title = "隣に座る細身巨乳な金髪ギャルに乳首を弄られた"
        bare = S._extract_title_theme_keywords(title)
        self.assertNotIn("映画館", bare, bare)
        stamped = S._recompute_theme_keywords(
            {"title": title, "genres": list(ROYD_CATALOG_GENRES), "series": ROYD_SERIES}
        )
        self.assertIn("映画館", stamped["theme_keywords"], stamped["theme_keywords"])
        self.assertIn("映画館", stamped["keyword_queries"], stamped["keyword_queries"])
        self.assertIn("平日昼間の映画館", stamped["keyword_queries"])
        self.assertNotIn("ROYAL", stamped["theme_keywords"])

    def test_cinema_keyword_bucket_is_not_thinner_with_missav(self):
        catalog = _cinema_catalog()
        seen: dict[str, list[str]] = {"javbus": [], "missav": []}

        def _run(label: str, keywords: list[str], series: str | None):
            def fake_fetch(q, actress=None):
                seen[label].append(q)
                return [dict(row) for row in catalog]

            def fake_enrich(c, why="片名候選"):
                item = dict(c)
                item["why"] = why
                item.setdefault("stills", [])
                return item

            with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_fetch), mock.patch.object(
                S, "fetch_jav321_title_results", return_value=[]
            ), mock.patch.object(
                S, "fetch_javlibrary_title_results", return_value=[]
            ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
                return S._find_related_by_keywords(
                    ROYD_TITLE,
                    auto_keywords=keywords,
                    series=series,
                    max_n=5,
                    budget_sec=6.0,
                    exclude_code="ROYD-343",
                )

        javbus_rows = _run("javbus", list(ROYD_WITH_CATALOG), None)
        missav_rows = _run("missav", list(ROYD_WITH_MISSAV), ROYD_SERIES)

        def cinema_codes(rows: list[dict]) -> list[str]:
            return [str(r.get("code")) for r in rows if "映画館" in str(r.get("title") or "")]

        javbus_cinema = cinema_codes(javbus_rows)
        missav_cinema = cinema_codes(missav_rows)
        self.assertGreaterEqual(len(javbus_cinema), 2, javbus_rows)
        self.assertGreaterEqual(len(missav_cinema), len(javbus_cinema), (javbus_cinema, missav_cinema))
        self.assertLessEqual(len(javbus_rows), 5)
        self.assertLessEqual(len(missav_rows), 5)
        self.assertIn("映画館", seen["javbus"], seen["javbus"])
        self.assertIn("映画館", seen["missav"], seen["missav"])
        self.assertIn("平日昼間の映画館", seen["missav"], seen["missav"])
        self.assertNotIn("平日昼間の映画館", seen["javbus"])


if __name__ == "__main__":
    unittest.main()
