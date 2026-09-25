#!/usr/bin/env python3
"""Extra Chinese-title listings. Site text only; nothing is translated."""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402

APGH_AVBEBE = (
    "【馬賽克破解】[APGH-012] 老師會在一對一私人補習中全程照顧。 柊由紀"
)
APGH_CLEAN = "老師會在一對一私人補習中全程照顧"

AVBEBE_HIT = f"""
<html><head>
<meta property="og:title" content="{APGH_AVBEBE}">
</head><body>
<h3 class="jeg_post_title">
  <a href="https://avbebe.com/archives/9">【馬賽克破解】[APGH-099] 這不是這一支的標題。 別人</a>
</h3>
<h3 class="jeg_post_title">
  <a href="https://avbebe.com/archives/214993">{APGH_AVBEBE}</a>
</h3>
</body></html>
"""

AVBEBE_EMPTY = """
<html><head>
<title>「DAS-034」的搜尋結果 &#8211; Avbebe 高清</title>
<meta property="og:title" content="「DAS-034」的搜尋結果 &#8211; Avbebe 高清">
</head><body>
<h1 class="jeg_archive_title">Search Result for &#039;DAS-034&#039;</h1>
</body></html>
"""

UNCENX_REAL = """
<html><head>
<meta property="og:title" content="REAL-852 UNCEN 巨乳泳隊成員媚藥訓練營 - 禰土和歌">
<title>REAL-852 UNCEN 巨乳泳隊成員媚藥訓練營 - 禰土和歌</title>
</head><body>
<h1>REAL-852 UNCEN 巨乳泳隊成員媚藥訓練營 - 禰土和歌</h1>
</body></html>
"""

MISSAV_REAL = (
    '<html><head><meta property="og:title" '
    'content="REAL-852 巨乳泳社 - MissAV"></head></html>'
)


class ZhListingTitleTests(unittest.TestCase):
    def test_avbebe_apgh012_title_cleans_to_chinese(self):
        self.assertEqual(
            S._clean_title_zh(APGH_AVBEBE, code="APGH-012", trusted=True),
            APGH_CLEAN,
        )
        self.assertNotIn("柊", APGH_CLEAN)
        self.assertNotIn("馬賽克", APGH_CLEAN)
        self.assertNotIn("APGH", APGH_CLEAN)

    def test_listing_chrome_strips_badges_and_keeps_wording(self):
        cases = {
            "APGH-012 UNCEN 兩個老師私人家教全包！柊木由希": "兩個老師私人家教全包",
            "REAL-852 UNCEN 巨乳泳隊成員媚藥訓練營 - 禰土和歌": "巨乳泳隊成員媚藥訓練營",
            "APGH-015 UNCEN 老師一人包辦一切的單獨家教 向日葵由良 - Hinata Yura": "老師一人包辦一切的單獨家教",
            "SONE-387 UNCEN 泳社巨乳妹成獵物…校泳裝撐不住的爆乳遭擠吸變態玩弄 木原美優": "泳社巨乳妹成獵物…校泳裝撐不住的爆乳遭擠吸變態玩弄",
            "【高清中字】[SONE-387] 盯上的巨乳游泳部員…從泳裝外露的成長期的胸部被獵奇般地揉捏玩弄… 清原美優": "盯上的巨乳游泳部員…從泳裝外露的成長期的胸部被獵奇般地揉捏玩弄…",
            "REAL-852 巨乳泳社 - MissAV": "巨乳泳社",
            "MIDA-616 女友妹妹的無胸罩誘惑 - MissAV": "女友妹妹的無胸罩誘惑",
            "NHDTC-235 夜行巴士逆NTR 痴女姐姐從座位縫隙偷偷給我男友打飛機 忍聲做愛直到中出 ~ 小野坂唯香 二羽紗愛 巴煇": "夜行巴士逆NTR 痴女姐姐從座位縫隙偷偷給我男友打飛機 忍聲做愛直到中出",
            "NHDTC-099 五個膽小的女孩在夜間巴士上睡覺時被人猥褻，嚇得睜不開眼，卻在假裝睡覺的同時失控地達到高潮——超大噴射特輯": "五個膽小的女孩在夜間巴士上睡覺時被人猥褻，嚇得睜不開眼，卻在假裝睡覺的同時失控地達到高潮——超大噴射特輯",
        }
        for raw, expected in cases.items():
            found = re.search(r"[A-Za-z]{2,10}-\d{2,5}", raw)
            got = S._clean_title_zh(raw, code=found.group(0), trusted=True)
            self.assertEqual(got, expected, raw)

    def test_avbebe_search_keeps_the_matching_code(self):
        self.assertEqual(
            S._parse_avbebe_search_html(AVBEBE_HIT, code="APGH-012"),
            APGH_CLEAN,
        )
        self.assertIsNone(S._parse_avbebe_search_html(AVBEBE_HIT, code="DAS-034"))

    def test_missing_source_stays_none(self):
        self.assertIsNone(S._parse_avbebe_search_html(AVBEBE_EMPTY, code="DAS-034"))
        self.assertIsNone(
            S._clean_title_zh(
                "「DAS-034」的搜尋結果 – Avbebe 高清",
                code="DAS-034",
                trusted=True,
            )
        )

        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "avbebe.com" in url and "DAS-034" in url:
                return AVBEBE_EMPTY
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("DAS-034", title_ja="日文題")
        self.assertIsNone(meta["title_zh"])
        self.assertEqual(meta["genres"], [])
        self.assertIsNone(meta["cover"])

    def test_missav_hit_does_not_call_listing_sites(self):
        seen = []

        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            seen.append(url)
            if "missav.ai/cn/real-852" in url:
                return MISSAV_REAL
            if "avbebe.com" in url or "uncenx.com" in url:
                raise AssertionError(url)
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("REAL-852", title_ja="巨乳水泳部員")
        self.assertEqual(meta["title_zh"], "巨乳泳社")
        self.assertTrue(any("missav.ai/cn/real-852" in u for u in seen))
        self.assertFalse(any("avbebe.com" in u or "uncenx.com" in u for u in seen))

    def test_uncenx_fills_title_without_tags_or_cover(self):
        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "uncenx.com/tw/real-852" in url:
                return UNCENX_REAL
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("REAL-852", title_ja="巨乳水泳部員")
        self.assertEqual(meta["title_zh"], "巨乳泳隊成員媚藥訓練營")
        self.assertEqual(meta["genres"], [])
        self.assertIsNone(meta["series"])
        self.assertIsNone(meta["cover"])
        self.assertIsNone(meta["actress_zh"])

    def test_avbebe_hit_stops_before_uncenx(self):
        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "avbebe.com" in url:
                return AVBEBE_HIT
            if "uncenx.com" in url or "javrate.com" in url:
                raise AssertionError(url)
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("APGH-012")
        self.assertEqual(meta["title_zh"], APGH_CLEAN)

    def test_javrate_card_title_for_the_matching_code(self):
        html = """
        <html><head>
        <title>nhdtc-235找到 - 1部A片 | </title>
        <meta property="og:title" content="nhdtc-235找到 - 1部A片 | ">
        </head><body>
        <h1>搜索 <label>nhdtc-235</label></h1>
        <a href="/movie/detail/other.html" title="APGH-099 別的作品 ~ 別人"></a>
        <a href="/movie/detail/16889a44-2fdf-416c-91fd-ade69b7cd626.html"
           title="NHDTC-235 &#x591C;&#x884C;&#x5DF4;&#x58EB;&#x9006;NTR &#x75F4;&#x5973;&#x59D0;&#x59D0;&#x5F9E;&#x5EA7;&#x4F4D;&#x7E2B;&#x9699;&#x5077;&#x5077;&#x7D66;&#x6211;&#x7537;&#x53CB;&#x6253;&#x98DB;&#x6A5F; &#x5FCD;&#x8072;&#x505A;&#x611B;&#x76F4;&#x5230;&#x4E2D;&#x51FA; ~ &#x5C0F;&#x91CE;&#x5742;&#x552F;&#x9999; &#x4E8C;&#x7FBD;&#x7D17;&#x611B; &#x5DF4;&#x7147;"
           class="movie-card-link"></a>
        </body></html>
        """
        self.assertEqual(
            S._parse_javrate_search_html(html, code="NHDTC-235"),
            "夜行巴士逆NTR 痴女姐姐從座位縫隙偷偷給我男友打飛機 忍聲做愛直到中出",
        )
        self.assertIsNone(S._parse_javrate_search_html(html, code="DAS-034"))
        self.assertIsNone(
            S._clean_title_zh("nhdtc-235找到 - 1部A片", code="NHDTC-235", trusted=True)
        )

        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "javrate.com/search/" in url and "NHDTC-235" in url.upper():
                return html
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("NHDTC-235", title_ja="日文題")
        self.assertEqual(
            meta["title_zh"],
            "夜行巴士逆NTR 痴女姐姐從座位縫隙偷偷給我男友打飛機 忍聲做愛直到中出",
        )
        self.assertEqual(meta["genres"], [])
        self.assertIsNone(meta["cover"])

    def test_short_cover_fetch_does_not_call_listings(self):
        def fake_get(url, timeout=8.0, headers=None, **kwargs):
            if "avbebe.com" in url or "uncenx.com" in url:
                raise AssertionError(url)
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("APGH-012", page_limit=2)
        self.assertIsNone(meta["title_zh"])

    @unittest.skipUnless(os.environ.get("LIVE_ZH_LISTING") == "1", "live network")
    def test_live_listing_titles_when_a_source_has_one(self):
        expect = {
            "APGH-012": "老師會在一對一私人補習中全程照顧",
            "REAL-852": "巨乳泳隊成員媚藥訓練營",
            "SONE-387": None,  # wording differs by site; any Chinese line is enough
            "APGH-015": "老師一人包辦一切的單獨家教",
        }
        for code, exact in expect.items():
            meta = S.fetch_public_zh_catalog(code, timeout=6.0)
            got = meta.get("title_zh")
            self.assertTrue(got, code)
            self.assertNotIn(code, got)
            if exact and got == exact:
                continue
            self.assertGreaterEqual(len(got), 4, (code, got))
        for code in ("DAS-034", "NHDTC-235"):
            meta = S.fetch_public_zh_catalog(code, timeout=6.0)
            got = meta.get("title_zh")
            if got:
                self.assertNotIn(code, got)
                self.assertFalse(got.startswith("（"))
                self.assertFalse(got.endswith("）") and len(got) < 3)


if __name__ == "__main__":
    unittest.main()
