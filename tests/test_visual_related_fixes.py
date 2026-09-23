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
        for tok in (
            "眼鏡",
            "メガネ",
            "眼鏡っ娘",
            "地味",
            "美人",
            "電車",
            "オフィス",
            "辦公室",
            "OL",
        ):
            self.assertIn(tok, S._THEME_KEYWORD_LEXICON, tok)
            self.assertFalse(S._is_weak_theme_token(tok), tok)
            self.assertNotIn(tok, S._WEAK_THEME_TOKENS, tok)
        self.assertNotIn("地位", S._THEME_KEYWORD_LEXICON)
        self.assertEqual(S._keyword_min_hits(["眼鏡", "地味", "美人", "OL"]), 2)
        self.assertEqual(S._keyword_min_hits(["眼鏡", "地味"]), 1)
        self.assertEqual(S._keyword_min_hits(["眼鏡"]), 1)

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
        self.assertIn("OL", distinctive)
        self.assertGreaterEqual(len(kws), 3)
        self.assertEqual(S._keyword_min_hits(kws), 2)
        self.assertGreaterEqual(S._keyword_hit_count("地味なメガネのOLが会社で", kws), 2)
        self.assertGreaterEqual(S._keyword_hit_count("眼鏡っ娘の美人OL", kws), 2)
        self.assertLess(S._keyword_hit_count("ただのOLです", kws), 2)

    def test_office_train_nouns_extract(self):
        kws = S._extract_title_theme_keywords("満員電車のオフィスで美人OL")
        for tok in ("電車", "オフィス", "美人", "OL", "満員"):
            self.assertIn(tok, kws, kws)
        self.assertIn("辦公室", S._extract_title_theme_keywords("辦公室的美人"))

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

    def test_two_keyword_title_allows_single_noun_hits_without_pad(self):
        title = "地味な眼鏡では隠し切れない"

        def fake_avbase(q, actress=None):
            return [
                {"code": "BBB-001", "title": "眼鏡っ娘の放課後", "actress": "A", "score": 0.6},
                {"code": "BBB-002", "title": "地味な日常", "actress": "B", "score": 0.5},
                {"code": "BBB-003", "title": "ただの中出し", "actress": "C", "score": 0.9},
            ]

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        kws = S._extract_title_theme_keywords(title)
        self.assertLessEqual(len(kws), 2, kws)
        self.assertIn("眼鏡", kws)
        self.assertIn("地味", kws)
        self.assertEqual(S._keyword_min_hits(kws), 1)

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_avbase), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S._find_related_by_keywords(title, max_n=5, budget_sec=20.0)
        codes = [r["code"] for r in rows]
        self.assertIn("BBB-001", codes)
        self.assertIn("BBB-002", codes)
        self.assertNotIn("BBB-003", codes)
        self.assertLessEqual(len(rows), 5)

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


class TestKeywordResearch(unittest.TestCase):
    TITLE = "地味な眼鏡では隠し切れない美人OLが性欲を抑えきれず完全生撮り"

    def test_selected_min_hits_and_original_rule_unchanged(self):
        self.assertEqual(S._selected_keyword_min_hits(["眼鏡", "地味", "OL"]), 2)
        self.assertEqual(S._selected_keyword_min_hits(["眼鏡", "地味"]), 2)
        self.assertEqual(S._selected_keyword_min_hits(["眼鏡"]), 1)
        self.assertEqual(S._selected_keyword_min_hits([]), 0)
        # Title bucket still allows a single hit when the title only yielded two nouns.
        self.assertEqual(S._keyword_min_hits(["眼鏡", "地味"]), 1)
        self.assertEqual(S._keyword_min_hits(["眼鏡", "地味", "美人", "OL"]), 2)

    def test_stamp_exposes_keywords_without_reguessing(self):
        payload = S._stamp_theme_keywords(
            {"title": self.TITLE, "actress": "楪カレン", "ok": True}
        )
        for tok in ("地味", "眼鏡", "美人", "OL"):
            self.assertIn(tok, payload["theme_keywords"], payload["theme_keywords"])
        self.assertTrue(payload["keyword_queries"])
        kept = S._stamp_theme_keywords(
            {
                "title": self.TITLE,
                "theme_keywords": ["眼鏡", "地味"],
                "keyword_queries": ["地味眼鏡"],
            }
        )
        self.assertEqual(kept["theme_keywords"], ["眼鏡", "地味"])
        self.assertEqual(kept["keyword_queries"], ["地味眼鏡"])

    def test_selected_only_queries_skip_unselected_nouns(self):
        qs = S._keyword_search_queries(self.TITLE, ["眼鏡"], selected_only=True)
        self.assertTrue(any("眼鏡" in q or "メガネ" in q for q in qs), qs)
        self.assertFalse(any("地味" in q for q in qs), qs)

    def _rows(self):
        return [
            {"code": "PRED-001", "title": "地味な眼鏡の会社員", "actress": "A", "score": 0.8},
            {"code": "PRED-002", "title": "メガネっ娘の地味な毎日", "actress": "B", "score": 0.7},
            {"code": "PRED-003", "title": "眼鏡っ娘の放課後", "actress": "C", "score": 0.9},
            {"code": "PRED-004", "title": "ただの中出し", "actress": "D", "score": 1.0},
            {"code": "JUFE-271", "title": "地味な眼鏡の美人", "actress": "E", "score": 1.0},
        ]

    def _run(self, keywords, **kwargs):
        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", return_value=self._rows()), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            return S._find_related_by_keywords(
                self.TITLE,
                exclude_code="JUFE-271",
                max_n=5,
                budget_sec=20.0,
                keywords=keywords,
                **kwargs,
            )

    def test_two_selected_keywords_require_multi_hit_no_pad(self):
        rows = self._run(["眼鏡", "地味"])
        codes = [r["code"] for r in rows]
        self.assertIn("PRED-001", codes)
        self.assertIn("PRED-002", codes)
        self.assertNotIn("PRED-003", codes, "single 眼鏡 hit must not fill a 2-keyword re-search")
        self.assertNotIn("PRED-004", codes)
        self.assertNotIn("JUFE-271", codes)
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(all(r.get("line") == "keyword" for r in rows))
        self.assertTrue(all(int(r.get("keyword_hits") or 0) >= 2 for r in rows))

    def test_one_selected_keyword_may_fill_without_junk(self):
        rows = self._run(["眼鏡"])
        codes = [r["code"] for r in rows]
        self.assertIn("PRED-003", codes)
        self.assertIn("PRED-001", codes)
        self.assertNotIn("PRED-004", codes)
        self.assertNotIn("JUFE-271", codes)
        self.assertLessEqual(len(rows), 5)

    def test_research_caps_at_five(self):
        many = [
            {"code": f"AAA-{i:03d}", "title": "地味な眼鏡の美人", "actress": "A", "score": 0.4}
            for i in range(1, 12)
        ]

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", return_value=many), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S._find_related_by_keywords(
                self.TITLE,
                keywords=["眼鏡", "地味"],
                max_n=5,
                budget_sec=20.0,
            )
        self.assertEqual(len(rows), 5)

    def test_api_payload_and_empty_selection(self):
        captured = {}

        def fake_find(title, **kwargs):
            captured["title"] = title
            captured.update(kwargs)
            return [
                {
                    "code": "PRED-001",
                    "title": "地味な眼鏡の会社員",
                    "title_zh": "土味眼鏡",
                    "line": "keyword",
                    "why": "關鍵字×2",
                    "stills": [],
                }
            ]

        with mock.patch.object(S, "_find_related_by_keywords", side_effect=fake_find), mock.patch.object(
            S, "attach_chinese_titles", side_effect=lambda payload, **kwargs: payload
        ):
            client = S.app.test_client()
            res = client.post(
                "/api/related-by-keywords",
                json={
                    "title": self.TITLE,
                    "code": "JUFE-271",
                    "actress": "楪カレン",
                    "keywords": ["眼鏡", "地味"],
                },
            )
            empty = client.post(
                "/api/related-by-keywords",
                json={"title": self.TITLE, "keywords": []},
            )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("keywords"), ["眼鏡", "地味"])
        self.assertEqual(data.get("min_hits"), 2)
        self.assertEqual(captured.get("keywords"), ["眼鏡", "地味"])
        self.assertEqual(captured.get("min_hits"), 2)
        self.assertEqual(len(data.get("related") or []), 1)
        self.assertEqual(data["related"][0]["code"], "PRED-001")
        self.assertIn("眼鏡", data.get("theme_keywords") or [])
        self.assertTrue(data.get("keyword_queries"))
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json().get("related"), [])
        self.assertEqual(empty.get_json().get("min_hits"), 0)
        self.assertIn("OL", empty.get_json().get("theme_keywords") or [])


class TestRelatedBucketOrder(unittest.TestCase):
    def test_actress_note_mentioning_theme_stays_after_keywords(self):
        items = [
            {"code": "SNIS-978", "line": "theme", "why": "同系列"},
            {
                "code": "MIDA-584",
                "line": "theme",
                "why": "同女優／同レーベル；義妹挑発アピールで主題線に近い",
            },
            {
                "code": "VENX-380",
                "line": "keyword",
                "why": "關鍵字×2",
                "keyword_hits": 2,
                "title": "ノーブラ巨乳叔母",
                "matched_keywords": ["巨乳"],
            },
            {"code": "MIDA-652", "line": "actress", "why": "同女優／同レーベル"},
        ]
        ordered = S._cap_related_buckets(items)
        lines = [x["line"] for x in ordered]
        self.assertEqual(lines, ["theme", "keyword", "actress", "actress"])
        codes = [x["code"] for x in ordered]
        self.assertLess(codes.index("SNIS-978"), codes.index("VENX-380"))
        self.assertLess(codes.index("VENX-380"), codes.index("MIDA-584"))
        self.assertEqual(ordered[2]["line"], "actress")
        self.assertEqual(S._related_line_of(items[1]), "actress")
        self.assertEqual(S._related_line_of(items[0]), "theme")
        self.assertEqual(S._related_line_of(items[2]), "keyword")

    def test_matched_keywords_are_real_hits_only(self):
        title = "地味なメガネのOLが会社で"
        matched = S._matched_theme_keywords(title, ["眼鏡", "地味", "美人", "OL"])
        self.assertEqual(matched, ["眼鏡", "地味", "OL"])
        self.assertNotIn("美人", matched)

    def test_mida616_keyword_block_is_not_split_by_actress(self):
        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        kw_rows = [
            {
                "code": "VENX-380",
                "title": "巨乳だけど系列は違う作品",
                "line": "keyword",
                "why": "關鍵字×2",
                "keyword_hits": 2,
                "matched_keywords": ["巨乳"],
            },
            {
                "code": "ZZZA-1241",
                "title": "別の巨乳作品パート2",
                "line": "keyword",
                "why": "關鍵字×1",
                "keyword_hits": 1,
                "matched_keywords": ["巨乳"],
            },
        ]
        demo_rows = [
            {"code": "SNIS-978", "title": "系列A", "line": "theme", "why": "同系列"},
            {"code": "SSNI-432", "title": "系列B", "line": "theme", "why": "同系列"},
            {
                "code": "MIDA-584",
                "title": "義妹",
                "line": "actress",
                "why": "同女優／同レーベル；義妹挑発アピールで主題線に近い",
            },
            {"code": "MIDA-652", "title": "痴女", "line": "actress", "why": "同女優／同レーベル"},
        ]

        with mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
            S, "fetch_avbase_title_results", return_value=[]
        ), mock.patch.object(S, "fetch_jav321_title_results", return_value=[]), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(
            S, "related_from_demo", return_value=demo_rows, create=True
        ), mock.patch.object(
            S, "_find_related_by_keywords", return_value=kw_rows
        ), mock.patch.object(
            S, "_find_related_by_actress", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S.find_related_by_title(
                "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク",
                exclude_code="MIDA-616",
                actress="福田ゆあ",
                budget_sec=30,
            )
        lines = [r.get("line") for r in rows]
        compact = "".join({"theme": "T", "keyword": "K", "actress": "A"}.get(ln, "?") for ln in lines)
        self.assertRegex(compact, r"^T*K*A*$", compact)
        codes = [r["code"] for r in rows]
        self.assertIn("SNIS-978", codes)
        self.assertIn("VENX-380", codes)
        self.assertIn("MIDA-584", codes)
        self.assertEqual(next(r["line"] for r in rows if r["code"] == "MIDA-584"), "actress")
        self.assertLess(codes.index("SNIS-978"), codes.index("VENX-380"))
        self.assertLess(codes.index("VENX-380"), codes.index("MIDA-584"))
        venx = next(r for r in rows if r["code"] == "VENX-380")
        self.assertIn("巨乳", venx.get("matched_keywords") or [])


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
