#!/usr/bin/env python3
"""Light unit tests for visual same_work enforcement and related caps."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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
                max_n=S.RELATED_KEYWORD_RESEARCH_CAP,
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
        self.assertLessEqual(len(rows), S.RELATED_KEYWORD_RESEARCH_CAP)
        self.assertLess(len(rows), S.RELATED_KEYWORD_RESEARCH_CAP, "must not pad to the cap")
        self.assertTrue(all(r.get("line") == "keyword" for r in rows))
        self.assertTrue(all(int(r.get("keyword_hits") or 0) >= 2 for r in rows))

    def test_one_selected_keyword_may_fill_without_junk(self):
        rows = self._run(["眼鏡"])
        codes = [r["code"] for r in rows]
        self.assertIn("PRED-003", codes)
        self.assertIn("PRED-001", codes)
        self.assertNotIn("PRED-004", codes)
        self.assertNotIn("JUFE-271", codes)
        self.assertLessEqual(len(rows), S.RELATED_KEYWORD_RESEARCH_CAP)
        self.assertLess(len(rows), S.RELATED_KEYWORD_RESEARCH_CAP, "must not pad to the cap")

    def test_research_caps_at_ten_and_carousel_stays_five(self):
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
            carousel = S._find_related_by_keywords(
                self.TITLE,
                keywords=["眼鏡", "地味"],
                max_n=S.RELATED_KEYWORD_CAP,
                budget_sec=20.0,
            )
            research = S._find_related_by_keywords(
                self.TITLE,
                keywords=["眼鏡", "地味"],
                max_n=S.RELATED_KEYWORD_RESEARCH_CAP,
                budget_sec=20.0,
            )
        self.assertEqual(S.RELATED_KEYWORD_CAP, 5)
        self.assertEqual(S.RELATED_KEYWORD_RESEARCH_CAP, 10)
        self.assertEqual(len(carousel), 5)
        # Fetch window is 10 candidates; return every real match up to the re-search cap.
        self.assertEqual(len(research), 10)
        self.assertLess(len(research), 12)

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
        self.assertEqual(captured.get("max_n"), S.RELATED_KEYWORD_RESEARCH_CAP)
        self.assertEqual(len(data.get("related") or []), 1)
        self.assertEqual(data["related"][0]["code"], "PRED-001")
        self.assertIn("眼鏡", data.get("theme_keywords") or [])
        self.assertTrue(data.get("keyword_queries"))
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json().get("related"), [])
        self.assertEqual(empty.get_json().get("min_hits"), 0)
        self.assertIn("OL", empty.get_json().get("theme_keywords") or [])

    def test_api_returns_real_matches_up_to_ten_without_padding(self):
        many = [
            {
                "code": f"AAA-{i:03d}",
                "title": "地味な眼鏡",
                "line": "keyword",
                "why": "關鍵字×2",
                "stills": [],
            }
            for i in range(1, 13)
        ]

        def fake_find(title, **kwargs):
            self.assertEqual(kwargs.get("max_n"), 10)
            n = 12 if kwargs.get("keywords") == ["眼鏡", "地味"] else 3
            return many[:n]

        with mock.patch.object(S, "_find_related_by_keywords", side_effect=fake_find), mock.patch.object(
            S, "attach_chinese_titles", side_effect=lambda payload, **kwargs: payload
        ):
            client = S.app.test_client()
            full = client.post(
                "/api/related-by-keywords",
                json={"title": self.TITLE, "code": "JUFE-271", "keywords": ["眼鏡", "地味"]},
            )
            short = client.post(
                "/api/related-by-keywords",
                json={"title": self.TITLE, "code": "JUFE-271", "keywords": ["眼鏡"]},
            )
        self.assertEqual(full.status_code, 200)
        full_rows = full.get_json().get("related") or []
        self.assertEqual(len(full_rows), 10)
        self.assertEqual(full_rows[0]["code"], "AAA-001")
        self.assertEqual(full_rows[-1]["code"], "AAA-010")
        short_rows = short.get_json().get("related") or []
        self.assertEqual([r["code"] for r in short_rows], ["AAA-001", "AAA-002", "AAA-003"])


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


class TestDistinctiveThemeKeywords(unittest.TestCase):
    """Compound phrases lead; relationship/action fluff does not outrank them."""

    MIDA = "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク"

    def test_mida616_prefers_compound_then_kyonyu(self):
        kws = S._extract_title_theme_keywords(self.MIDA, actress="福田ゆあ")
        self.assertGreaterEqual(len(kws), 2, kws)
        self.assertEqual(kws[0], "ノーブラ誘惑", kws)
        self.assertEqual(kws[1], "巨乳", kws)
        # Noun half of the compound stays, but does not outrank 巨乳.
        self.assertIn("ノーブラ", kws)
        self.assertGreater(kws.index("ノーブラ"), kws.index("巨乳"))
        self.assertLess(kws.index("ノーブラ誘惑"), kws.index("ノーブラ"))
        for rel in ("彼女の妹", "彼女", "妹"):
            self.assertIn(rel, kws, kws)
            self.assertGreater(kws.index(rel), kws.index("巨乳"), kws)
            self.assertGreater(kws.index(rel), kws.index("ノーブラ誘惑"), kws)
        self.assertLess(kws.index("彼女の妹"), kws.index("彼女"))
        self.assertLess(kws.index("彼女"), kws.index("妹"))
        # Bare 誘惑 is only the action half. ボク is not part of an XのY relation pair here.
        for absent in ("誘惑", "負け", "ボク", "私"):
            self.assertNotIn(absent, kws, kws)
        # noun+沼 in this title is kept, behind the circled theme nouns.
        self.assertIn("ナマ乳沼", kws)
        self.assertGreater(kws.index("ナマ乳沼"), kws.index("巨乳"))

    def test_mida616_queries_led_by_distinctive_terms(self):
        kws = S._extract_title_theme_keywords(self.MIDA)
        qs = S._keyword_search_queries(self.MIDA, kws)
        self.assertTrue(qs, qs)
        self.assertLessEqual(len(qs), 8)
        self.assertEqual(qs[0], "ノーブラ誘惑", qs)
        self.assertIn("巨乳", qs[:4])
        self.assertNotIn("彼女", qs)
        self.assertNotIn("妹", qs)
        self.assertNotIn("彼女の妹", qs)
        self.assertNotIn("誘惑", qs)
        selected = S._keyword_search_queries(
            self.MIDA, ["彼女の妹", "妹"], selected_only=True
        )
        self.assertIn("彼女の妹", selected)
        self.assertIn("妹", selected)
        self.assertLessEqual(len(selected), 10)
        self.assertTrue(all(not S._is_weak_theme_token(q) for q in qs[:2]), qs)

    def test_particle_blocks_false_compound(self):
        kws = S._extract_title_theme_keywords("ノーブラの誘惑に負けた巨乳")
        self.assertIn("ノーブラ", kws)
        self.assertIn("巨乳", kws)
        self.assertNotIn("ノーブラ誘惑", kws)
        self.assertNotIn("誘惑", kws)
        self.assertLess(kws.index("ノーブラ"), kws.index("彼女") if "彼女" in kws else 99)

    def test_kyonyu_swamp_keeps_noun_half(self):
        title = "巨乳沼にハマった眼鏡OL"
        kws = S._extract_title_theme_keywords(title)
        self.assertEqual(kws[0], "巨乳沼", kws)
        self.assertIn("巨乳", kws)
        self.assertIn("眼鏡", kws)
        self.assertIn("OL", kws)
        self.assertLess(kws.index("巨乳沼"), kws.index("巨乳"))
        self.assertNotIn("沼", kws)
        qs = S._keyword_search_queries(title, kws)
        self.assertEqual(qs[0], "巨乳沼", qs)
        self.assertLessEqual(len(qs), 8)
        self.assertTrue(any(q == "メガネ" or "眼鏡" in q for q in qs), qs)

    def test_megane_alias_and_jufe_scraps_unchanged(self):
        title = "地味な眼鏡では隠し切れない美人OLが性欲を抑えきれず完全生撮り"
        kws = S._extract_title_theme_keywords(title, actress="楪カレン")
        for tok in ("地味", "眼鏡", "美人", "OL"):
            self.assertIn(tok, kws, kws)
        for scrap in ("は隠し切れな", "が性欲を抑え", "きれず完全生"):
            self.assertNotIn(scrap, kws, kws)
        self.assertEqual(kws[0], "地味", kws)
        qs = S._keyword_search_queries(title, kws)
        self.assertIn("地味", qs)
        self.assertTrue(any("眼鏡" in q or "メガネ" in q for q in qs), qs)
        self.assertLessEqual(len(qs), 8)
        selected = S._keyword_search_queries(title, ["眼鏡"], selected_only=True)
        self.assertLessEqual(len(selected), 10)
        self.assertTrue(any("眼鏡" in q or "メガネ" in q for q in selected), selected)
        self.assertFalse(any("地味" in q for q in selected), selected)

    def test_weak_relation_does_not_outrank_theme_noun(self):
        kws = S._extract_title_theme_keywords("彼女の妹とボクの巨乳電車")
        self.assertIn("巨乳", kws)
        self.assertIn("電車", kws)
        for rel in ("彼女の妹", "彼女", "妹"):
            self.assertIn(rel, kws, kws)
            self.assertGreater(kws.index(rel), kws.index("巨乳"))
            self.assertGreater(kws.index(rel), kws.index("電車"))
        # ボクの巨乳 is not a relationship compound, so ボク is not a chip.
        self.assertNotIn("ボク", kws)
        self.assertNotIn("ボクの巨乳", kws)
        self.assertTrue(S._is_weak_theme_token("彼女"))
        self.assertTrue(S._is_weak_theme_token("誘惑"))
        self.assertTrue(S._is_weak_theme_token("妹"))
        self.assertFalse(S._is_weak_theme_token("ノーブラ"))
        self.assertFalse(S._is_weak_theme_token("巨乳"))
        self.assertFalse(S._is_weak_theme_token("義妹"))

    def test_kanojo_imouto_scores_compound_above_either_half(self):
        kws = ["ノーブラ誘惑", "巨乳", "ノーブラ", "彼女の妹", "彼女", "妹"]
        both = S._keyword_overlap("彼女の妹のノーブラ巨乳", kws)
        only_phrase_theme = S._keyword_overlap("彼女の妹と巨乳", kws)
        only_kanojo = S._keyword_overlap("彼女の日常と巨乳", kws)
        only_imouto = S._keyword_overlap("妹の部屋で巨乳", kws)
        theme_only = S._keyword_overlap("ノーブラの巨乳", kws)
        lone = S._keyword_overlap("彼女の日常", kws)
        # hits, score, matched, theme_hits
        self.assertIn("彼女の妹", both[2])
        self.assertIn("彼女", both[2])
        self.assertIn("妹", both[2])
        self.assertGreater(both[0], only_kanojo[0])
        self.assertGreater(both[1], only_kanojo[1])
        self.assertGreater(only_phrase_theme[1], only_kanojo[1])
        self.assertGreater(only_phrase_theme[1], only_imouto[1])
        self.assertGreater(only_kanojo[1], 0)
        self.assertGreater(only_imouto[1], 0)
        self.assertGreater(theme_only[1], lone[1])
        self.assertEqual(lone[3], 0)
        self.assertGreater(theme_only[3], 0)
        # 義妹 must not count as bare 妹.
        self.assertNotIn("妹", S._matched_theme_keywords("義妹の誘惑", ["妹"]))

    def test_relation_only_does_not_pad_ahead_of_theme_hits(self):
        def fake_avbase(q, actress=None):
            return [
                {"code": "AAA-001", "title": "彼女の日常", "actress": "A", "score": 1.0},
                {"code": "AAA-002", "title": "妹と過ごす夏", "actress": "B", "score": 1.0},
                {"code": "AAA-003", "title": "彼女の妹と温泉", "actress": "C", "score": 1.0},
                {"code": "AAA-004", "title": "ノーブラの巨乳", "actress": "D", "score": 0.2},
                {"code": "AAA-005", "title": "彼女の妹のノーブラ巨乳", "actress": "E", "score": 0.1},
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
            rows = S._find_related_by_keywords(self.MIDA, max_n=5, budget_sec=20.0)
        codes = [r["code"] for r in rows]
        self.assertEqual(codes[0], "AAA-005", codes)
        self.assertIn("AAA-004", codes)
        self.assertLess(codes.index("AAA-005"), codes.index("AAA-004"))
        for skipped in ("AAA-001", "AAA-002", "AAA-003"):
            self.assertNotIn(skipped, codes)
        top = rows[0]
        self.assertIn("彼女の妹", top.get("matched_keywords") or [])
        self.assertIn("巨乳", top.get("matched_keywords") or [])
        self.assertGreater(
            int(top.get("keyword_hits") or 0),
            int(next(r for r in rows if r["code"] == "AAA-004").get("keyword_hits") or 0),
        )


class TestKinshipRoleAndEditionMarkers(unittest.TestCase):
    """DANDYA-style titles: kinship chips, 教育 compounds, edition marks stripped.

    OL stays a lexicon occupation chip. It is absent on the DANDY title only
    because that title never says OL; VOL.2 must not invent it.
    """

    DANDYA = (
        "「今日も息子の家庭教師とセックスしています」2人きりになったら10秒で挿入 ? ! "
        "息子がすぐ隣にいるのにイケメン家庭教師のチ〇ポを握る肉欲教育ママVOL.2"
    )

    def test_dandya001_chips_and_auto_lead(self):
        kws = S._extract_title_theme_keywords(self.DANDYA, actress="大浦真奈美")
        self.assertEqual(
            kws,
            ["家庭教師", "10秒挿入", "肉欲教育", "息子の家庭教師", "息子", "ママ"],
            kws,
        )
        # This title has VOL.2 and no occupation OL, so neither is a chip.
        for absent in ("VOL", "OL", "Vol", "教育", "肉欲", "まま"):
            self.assertNotIn(absent, kws, kws)
        self.assertIn("OL", S._THEME_KEYWORD_LEXICON)
        self.assertIn("OL", S._SHORT_THEME_NOUNS)
        self.assertFalse(S._is_weak_theme_token("OL"))
        self.assertTrue(S._is_weak_theme_token("息子"))
        self.assertTrue(S._is_weak_theme_token("ママ"))
        self.assertFalse(S._is_weak_theme_token("家庭教師"))
        self.assertFalse(S._is_auto_theme_keyword("息子"))
        self.assertFalse(S._is_auto_theme_keyword("ママ"))
        self.assertFalse(S._is_auto_theme_keyword("息子の家庭教師"))
        self.assertTrue(S._is_auto_theme_keyword("家庭教師"))
        self.assertTrue(S._is_auto_theme_keyword("肉欲教育"))
        self.assertIn("10秒挿入", kws)
        self.assertTrue(S._is_auto_theme_keyword("10秒挿入"))
        self.assertFalse(S._is_weak_theme_token("10秒挿入"))
        self.assertGreaterEqual(S._keyword_hit_count("10秒で挿入", ["10秒挿入"]), 1)
        self.assertGreaterEqual(S._keyword_hit_count("１０秒に挿入", ["10秒挿入"]), 1)
        self.assertEqual(S._keyword_hit_count("110秒で挿入", ["10秒挿入"]), 0)
        self.assertNotIn("挿入", kws)
        qs = S._keyword_search_queries(self.DANDYA, kws)
        self.assertTrue(qs, qs)
        self.assertEqual(qs[0], "家庭教師", qs)
        self.assertIn("肉欲教育", qs)
        self.assertIn("10秒挿入", qs)
        self.assertIn("10秒で挿入", qs)
        self.assertGreater(qs.index("肉欲教育"), 0)
        for absent in ("息子", "ママ", "息子の家庭教師", "VOL", "OL"):
            self.assertNotIn(absent, qs, qs)
        selected = S._keyword_search_queries(self.DANDYA, ["息子", "ママ"], selected_only=True)
        self.assertIn("息子", selected)
        self.assertIn("ママ", selected)
        self.assertLessEqual(len(selected), 10)

    def test_edition_marker_and_latin_boundary(self):
        for title in (
            "美人OLの物語VOL.2",
            "美人OLの物語Vol.2",
            "美人OLの物語vol.2",
            "美人OLの物語VOL2",
            "美人OLの物語VOLUME 2",
        ):
            kws = S._extract_title_theme_keywords(title)
            self.assertIn("OL", kws, (title, kws))
            self.assertIn("美人", kws, (title, kws))
            self.assertNotIn("VOL", kws, (title, kws))
            self.assertNotIn("VOLUME", kws, (title, kws))
        for title in ("シリーズ新作VOL.2", "GOLD VOL.2", "COOL Vol.2"):
            kws = S._extract_title_theme_keywords(title)
            self.assertNotIn("OL", kws, (title, kws))
            self.assertNotIn("VOL", kws, (title, kws))
        self.assertIn("OL", S._extract_title_theme_keywords("美人OL"))
        self.assertEqual(S._keyword_hit_count("新作VOL.2", ["OL"]), 0)
        self.assertEqual(S._keyword_hit_count("COOLな毎日", ["OL"]), 0)
        self.assertGreaterEqual(S._keyword_hit_count("美人OL", ["OL"]), 1)
        self.assertGreaterEqual(S._keyword_hit_count("巨乳OL", ["OL"]), 1)
        # Numbered Japanese volume/episode counters are junk, not chips.
        # A real OL next to one of them still surfaces.
        for title in (
            "美人OL第2巻",
            "美人OL第２巻",
            "美人OL第十二巻",
            "美人OL第2話",
            "美人OL第2回",
            "美人OL EP.2",
            "美人OL Vol",
        ):
            kws = S._extract_title_theme_keywords(title)
            self.assertIn("OL", kws, (title, kws))
            self.assertIn("美人", kws, (title, kws))
            for tok in kws:
                self.assertNotIn("巻", tok, (title, kws))
                self.assertNotIn("話", tok, (title, kws))
                self.assertNotIn("回", tok, (title, kws))
                self.assertFalse(tok.upper() in {"VOL", "EP", "EPISODE", "VOLUME"}, (title, kws))
        bare = S._extract_title_theme_keywords("ただの日常第十二巻")
        self.assertNotIn("第十二巻", bare, bare)
        self.assertNotIn("第十二", bare, bare)
        self.assertEqual(S._extract_title_theme_keywords("第2巻"), [])
        self.assertEqual(S._extract_title_theme_keywords("VOL.2"), [])

    def test_education_suffix_on_other_titles(self):
        # Known lexicon head + 教育 leads, same rank rule as ノーブラ誘惑.
        kws = S._extract_title_theme_keywords("巨乳女教師の羞恥教育")
        self.assertIn("羞恥教育", kws, kws)
        self.assertIn("巨乳", kws)
        self.assertIn("女教師", kws)
        self.assertIn("羞恥", kws)
        self.assertNotIn("教育", kws)
        self.assertEqual(kws[0], "羞恥教育", kws)
        qs = S._keyword_search_queries("巨乳女教師の羞恥教育", kws)
        self.assertEqual(qs[0], "羞恥教育", qs)

        # Unknown 2-kanji head still forms a compound, but does not outrank theme nouns.
        title = "オフィスで性欲教育される秘書"
        kws2 = S._extract_title_theme_keywords(title)
        self.assertIn("性欲教育", kws2, kws2)
        self.assertIn("オフィス", kws2)
        self.assertIn("秘書", kws2)
        self.assertNotIn("教育", kws2)
        self.assertNotIn("性欲", kws2)
        self.assertEqual(kws2[0], "オフィス", kws2)
        self.assertLess(kws2.index("オフィス"), kws2.index("性欲教育"))
        self.assertLess(kws2.index("秘書"), kws2.index("性欲教育"))

        # Particle blocks the glue, same as ノーブラの誘惑. Bare leading 教育 is not a chip.
        parted = S._extract_title_theme_keywords("肉欲の教育ママ")
        self.assertNotIn("肉欲教育", parted, parted)
        self.assertIn("ママ", parted)
        self.assertNotIn("教育", parted)
        bare = S._extract_title_theme_keywords("教育ママ")
        self.assertEqual(bare, ["ママ"], bare)

    def test_kinship_occupation_exposes_parts_without_leading(self):
        kws = S._extract_title_theme_keywords("妹の家庭教師")
        self.assertEqual(kws[0], "家庭教師", kws)
        for rel in ("妹の家庭教師", "妹"):
            self.assertIn(rel, kws, kws)
            self.assertGreater(kws.index(rel), kws.index("家庭教師"))
        qs = S._keyword_search_queries("妹の家庭教師", kws)
        self.assertEqual(qs[0], "家庭教師", qs)
        self.assertNotIn("妹", qs)

        secret = S._extract_title_theme_keywords("彼女の秘書")
        self.assertEqual(secret[0], "秘書", secret)
        self.assertIn("彼女の秘書", secret)
        self.assertIn("彼女", secret)
        self.assertGreater(secret.index("彼女"), secret.index("秘書"))

        # Clothing / body / pronouns stay out of the occupation pattern.
        nobra = S._extract_title_theme_keywords("妹のノーブラ")
        self.assertIn("ノーブラ", nobra)
        self.assertNotIn("妹のノーブラ", nobra)
        self.assertNotIn("妹", nobra)
        boku = S._extract_title_theme_keywords("ボクの女医と巨乳")
        self.assertIn("女医", boku)
        self.assertIn("巨乳", boku)
        self.assertNotIn("ボク", boku)
        self.assertNotIn("ボクの女医", boku)

        full = S._keyword_overlap(
            "息子の家庭教師と肉欲教育",
            ["家庭教師", "肉欲教育", "息子の家庭教師", "息子", "ママ"],
        )
        half = S._keyword_overlap(
            "息子と家庭教師",
            ["家庭教師", "肉欲教育", "息子の家庭教師", "息子", "ママ"],
        )
        self.assertIn("息子の家庭教師", full[2])
        self.assertNotIn("息子の家庭教師", half[2])
        self.assertGreater(full[1], half[1])

    def test_time_action_hook_is_general_and_not_edition_junk(self):
        hooked = S._extract_title_theme_keywords("3分で絶頂する美人OL")
        self.assertIn("3分絶頂", hooked, hooked)
        self.assertIn("美人", hooked)
        self.assertIn("OL", hooked)
        self.assertEqual(S._extract_title_theme_keywords("１０秒で挿入"), ["10秒挿入"])
        self.assertEqual(S._extract_title_theme_keywords("10秒挿入する"), ["10秒挿入"])
        plain = S._extract_title_theme_keywords("10秒で隣にいる")
        self.assertNotIn("10秒挿入", plain)
        self.assertNotIn("10秒隣", plain)
        for title in ("VOL.2", "第2巻", "美人OL第2巻", "美人OL"):
            kws = S._extract_title_theme_keywords(title)
            self.assertFalse(any(S._is_time_action_keyword(k) for k in kws), (title, kws))
        ol = S._extract_title_theme_keywords("美人OLの物語VOL.2")
        self.assertIn("OL", ol)
        self.assertNotIn("VOL", ol)
        self.assertFalse(any(S._is_time_action_keyword(k) for k in ol), ol)


class TestDandyCodeLookupAndVisualLock(unittest.TestCase):
    """Code-only DANDY lookup must restamp chips and fill 同女優.

    Image identify must keep a cover/still lock, and say when sort-only is
    the real outcome.
    """

    DANDY_TITLE = (
        "「今日も息子の家庭教師とセックスしています」2人きりになったら10秒で挿入 ? ! "
        "息子がすぐ隣にいるのにイケメン家庭教師のチ〇ポを握る肉欲教育ママVOL.2"
    )
    EXPECTED_KEYWORDS = ["家庭教師", "10秒挿入", "肉欲教育", "息子の家庭教師", "息子", "ママ"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_cache = S._OFFLINE_CACHE_PATH
        S._OFFLINE_CACHE_PATH = Path(self.tmp.name) / "offline-cache.json"

    def tearDown(self):
        S._OFFLINE_CACHE_PATH = self._prev_cache
        self.tmp.cleanup()

    def _write_stale_cache(self, code: str) -> None:
        entry_id = S._offline_cache_entry_id(code)
        display = S.format_display_code(code)
        data = {
            "version": 1,
            "by_key": {f"code:{display}": entry_id},
            "entries": {
                entry_id: {
                    "ok": True,
                    "code": display,
                    "title": self.DANDY_TITLE,
                    "title_zh": "中文主標",
                    "actress": "",
                    "studio": "DANDY",
                    "cover": "https://example.com/c.jpg",
                    "stills": ["https://example.com/s.jpg"],
                    "related_by_title": [
                        {
                            "code": "DANDY-100",
                            "title": self.DANDY_TITLE,
                            "title_zh": "中文相關",
                            "line": "theme",
                            "why": "片名相近",
                            "cover": "https://example.com/r.jpg",
                        }
                    ],
                    "theme_keywords": ["家庭教師", "OL", "VOL"],
                    "keyword_queries": ["家庭教師", "OL"],
                    "message": "舊快取",
                    "touched_at": 1,
                }
            },
        }
        S._OFFLINE_CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _catalog(self, code: str) -> dict:
        return {
            "code": S.format_display_code(code),
            "title": self.DANDY_TITLE,
            "actress": "竹内夏希",
            "studio": "DANDY",
            "source": "avbase",
            "cid": "1dandy00893",
            "cover": "https://example.com/c.jpg",
        }

    def _siblings(self):
        return [
            {"code": "DANDY-710", "title": "別作品A", "actress": "竹内夏希", "score": 0.4},
            {"code": "DANDY-711", "title": "別作品B", "actress": "竹内夏希", "score": 0.3},
            {"code": "DANDY-712", "title": "別作品C", "actress": "竹内夏希", "score": 0.2},
            {"code": "JUNK-009", "title": "無関係な作品", "actress": "別人", "score": 0.99},
        ]

    def _enrich_copy(self, c, why="片名候選"):
        item = dict(c)
        item["why"] = why
        item.setdefault("stills", [])
        item.setdefault("cover", "https://example.com/x.jpg")
        return item

    def test_recompute_replaces_stale_chips(self):
        payload = S._recompute_theme_keywords(
            {
                "title": self.DANDY_TITLE,
                "actress": "竹内夏希",
                "theme_keywords": ["家庭教師", "OL", "VOL"],
                "keyword_queries": ["OL"],
            }
        )
        self.assertEqual(payload["theme_keywords"], self.EXPECTED_KEYWORDS)
        for absent in ("OL", "VOL", "Vol", "教育"):
            self.assertNotIn(absent, payload["theme_keywords"])

    def test_actress_bucket_drops_non_matches_and_caps_at_three(self):
        def fake_avbase(q, actress=None):
            return self._siblings()

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_avbase), mock.patch.object(
            S, "enrich_title_candidate", side_effect=self._enrich_copy
        ):
            rows = S._find_related_by_actress(
                "竹内夏希",
                exclude_code="DANDY-893",
                max_n=3,
                budget_sec=5,
            )
        codes = [r["code"] for r in rows]
        self.assertEqual(codes, ["DANDY-710", "DANDY-711", "DANDY-712"])
        self.assertNotIn("JUNK-009", codes)
        self.assertTrue(all(r.get("line") == "actress" for r in rows))
        self.assertLessEqual(len(rows), 3)

    def test_actress_search_runs_after_title_budget_is_spent(self):
        called = {}

        def fake_actress(name, **kwargs):
            called["name"] = name
            return [self._enrich_copy(self._siblings()[0], why="同演員")]

        with mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
            S, "fetch_avbase_title_results", return_value=[]
        ), mock.patch.object(S, "_find_related_by_keywords", return_value=[]), mock.patch.object(
            S, "_find_related_by_actress", side_effect=fake_actress
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=self._enrich_copy):
            out = S.find_related_by_title(
                self.DANDY_TITLE,
                exclude_code="DANDY-893",
                actress="竹内夏希",
                budget_sec=0.01,
            )
        self.assertEqual(called.get("name"), "竹内夏希")
        self.assertTrue(any(r.get("line") == "actress" for r in out))
        self.assertLessEqual(sum(1 for r in out if r.get("line") == "actress"), 3)

    def test_code_cache_restamps_keywords_and_fills_actress(self):
        for code in ("DANDY-893", "DANDYA-001"):
            self._write_stale_cache(code)

            def fake_actress(name, **kwargs):
                self.assertEqual(name, "竹内夏希")
                return [dict(row) for row in self._siblings() if row["actress"] == "竹内夏希"]

            with mock.patch.object(
                S, "fetch_avbase_by_code", return_value=self._catalog(code)
            ), mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
                S, "fetch_avbase_title_results", return_value=[]
            ), mock.patch.object(S, "_find_related_by_keywords", return_value=[]), mock.patch.object(
                S, "_find_related_by_actress", side_effect=fake_actress
            ), mock.patch.object(
                S, "enrich_title_candidate", side_effect=self._enrich_copy
            ), mock.patch.object(S, "resolve_chinese_title", return_value=None):
                out = S.identify_code(code)
            self.assertEqual(out.get("actress"), "竹内夏希", code)
            self.assertEqual(out.get("theme_keywords"), self.EXPECTED_KEYWORDS, code)
            rel = out.get("related_by_title") or []
            actress_rows = [r for r in rel if r.get("line") == "actress"]
            self.assertGreaterEqual(len(actress_rows), 1, rel)
            self.assertLessEqual(len(actress_rows), 3)
            codes = [r.get("code") for r in rel]
            self.assertNotIn("JUNK-009", codes)
            self.assertIn("DANDY-100", codes)
            self.assertLessEqual(len(rel), 13)

    def test_attach_refills_actress_when_related_already_exists(self):
        payload = {
            "ok": True,
            "code": "DANDY-893",
            "title": self.DANDY_TITLE,
            "title_zh": "中文主標",
            "actress": "竹内夏希",
            "cover": "https://example.com/c.jpg",
            "theme_keywords": ["家庭教師"],
            "keyword_queries": ["家庭教師"],
            "related_by_title": [
                {
                    "code": "DANDY-100",
                    "title": self.DANDY_TITLE,
                    "title_zh": "中文相關",
                    "line": "theme",
                    "why": "片名相近",
                    "cover": "https://example.com/r.jpg",
                }
            ],
        }

        def fake_actress(name, **kwargs):
            return [dict(row) for row in self._siblings() if row["actress"] == "竹内夏希"]

        with mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
            S, "fetch_avbase_title_results", return_value=[]
        ), mock.patch.object(S, "_find_related_by_keywords", return_value=[]), mock.patch.object(
            S, "_find_related_by_actress", side_effect=fake_actress
        ), mock.patch.object(
            S, "enrich_title_candidate", side_effect=self._enrich_copy
        ), mock.patch.object(
            S, "attach_chinese_titles", side_effect=lambda payload, **kwargs: payload
        ):
            out = S.attach_related_by_title(payload, budget_sec=2, per_item=False)
        self.assertEqual(out.get("theme_keywords"), self.EXPECTED_KEYWORDS)
        actress_rows = [r for r in out.get("related_by_title") or [] if r.get("line") == "actress"]
        self.assertEqual(len(actress_rows), 3)
        self.assertIn("DANDY-100", [r.get("code") for r in out["related_by_title"]])

    def test_related_note_matches_carousel(self):
        dishonest = "線上目錄未取得同女優相關；僅顯示主作品 CDN。"
        with_actress = S._finalize_related_note(
            {
                "related_note": dishonest + "；視覺鎖定",
                "actress": "大浦真奈美",
                "related_by_title": [
                    {"code": "MDBK-434", "line": "actress", "why": "同演員", "title": "別作品"},
                    {"code": "AAA-001", "line": "theme", "why": "片名相近", "title": "系列"},
                ],
            }
        )
        note = with_actress.get("related_note") or ""
        self.assertNotIn("未取得同女優", note)
        self.assertNotIn("僅顯示主作品", note)
        self.assertIn("視覺鎖定", note)

        theme_only = S._finalize_related_note(
            {
                "related_note": dishonest,
                "actress": "大浦真奈美",
                "related_by_title": [
                    {"code": "AAA-001", "line": "theme", "why": "片名相近", "title": "系列"},
                ],
            }
        )
        theme_note = theme_only.get("related_note") or ""
        self.assertIn("未取得同女優", theme_note)
        self.assertNotIn("僅顯示主作品", theme_note)

        empty = S._finalize_related_note(
            {
                "related_note": dishonest,
                "actress": "大浦真奈美",
                "related_by_title": [],
            }
        )
        empty_note = empty.get("related_note") or ""
        self.assertIn("未取得同女優", empty_note)
        self.assertIn("僅顯示主作品", empty_note)

    def _keyword_rows(self, n=2):
        rows = []
        for i in range(n):
            rows.append(
                {
                    "code": f"KW-{i + 1:03d}",
                    "title": f"別シリーズの家庭教師が10秒で挿入 {i}",
                    "actress": "別人",
                    "score": 0.4,
                    "why": "關鍵字×2",
                    "line": "keyword",
                    "keyword_hits": 2,
                    "matched_keywords": ["家庭教師", "10秒挿入"],
                }
            )
        return rows

    def _pipeline_mocks(self, *, search_hit, keyword_rows=None, actress_rows=None):
        keyword_rows = self._keyword_rows() if keyword_rows is None else keyword_rows
        actress_rows = actress_rows if actress_rows is not None else [
            {"code": "MDBK-434", "title": "揺れる尻", "actress": "大浦真奈美", "score": 0.4},
        ]

        def fake_search(title, actress=None):
            return search_hit

        def fake_keywords(*args, **kwargs):
            return [dict(row) for row in keyword_rows]

        def fake_actress(name, **kwargs):
            return [dict(row) for row in actress_rows]

        patches = [
            mock.patch.object(S, "fetch_avbase_by_code", return_value=self._catalog("DANDYA-001")),
            mock.patch.object(
                S,
                "sanitize_cover_fields",
                return_value=("1dandya00001", "https://example.com/c.jpg", ["https://example.com/s.jpg"]),
            ),
            mock.patch.object(S, "resolve_chinese_title", return_value=None),
            mock.patch.object(S, "search_by_title", side_effect=fake_search),
            mock.patch.object(S, "fetch_avbase_title_results", return_value=[]),
            mock.patch.object(S, "_find_related_by_keywords", side_effect=fake_keywords),
            mock.patch.object(S, "_find_related_by_actress", side_effect=fake_actress),
            mock.patch.object(S, "enrich_title_candidate", side_effect=self._enrich_copy),
        ]
        return patches

    def _assert_dandya_buckets(self, result):
        self.assertEqual(result.get("theme_keywords"), self.EXPECTED_KEYWORDS)
        for tok in ("家庭教師", "息子", "ママ", "肉欲教育", "息子の家庭教師", "10秒挿入"):
            self.assertIn(tok, result.get("theme_keywords") or [], result.get("theme_keywords"))
        rel = result.get("related_by_title") or []
        lines = [r.get("line") for r in rel]
        self.assertIn("keyword", lines, rel)
        kw = [r for r in rel if r.get("line") == "keyword"]
        theme = [r for r in rel if r.get("line") == "theme"]
        actress = [r for r in rel if r.get("line") == "actress"]
        self.assertLessEqual(len(kw), 5)
        self.assertLessEqual(len(theme), 5)
        self.assertLessEqual(len(actress), 3)
        self.assertGreaterEqual(len(kw), 1)
        note = result.get("related_note") or ""
        if actress:
            self.assertNotIn("未取得同女優", note)
            self.assertNotIn("僅顯示主作品", note)
        return rel

    def test_code_lookup_stamps_keywords_and_honest_note(self):
        hit = {
            "code": "DANDYA-001",
            "title": self.DANDY_TITLE,
            "actress": "大浦真奈美",
            "score": 0.9,
            "candidates": [
                {
                    "code": "DANDY-900",
                    "title": self.DANDY_TITLE,
                    "actress": "大浦真奈美",
                    "score": 0.8,
                }
            ],
        }
        with contextlib.ExitStack() as stack:
            for p in self._pipeline_mocks(search_hit=hit):
                stack.enter_context(p)
            result, status = S.run_identify_pipeline(user_code="DANDYA-001")
        self.assertEqual(status, 200)
        self.assertEqual(result.get("code"), "DANDYA-001")
        rel = self._assert_dandya_buckets(result)
        self.assertTrue(any(r.get("line") == "actress" for r in rel), rel)
        self.assertIn("DANDY-900", [r.get("code") for r in rel])

    def test_title_lookup_stamps_keywords_and_keyword_bucket(self):
        hit = {
            "code": "DANDYA-001",
            "title": self.DANDY_TITLE,
            "actress": "大浦真奈美",
            "studio": "DANDY",
            "source": "avbase",
            "score": 0.9,
            "candidates": [
                {"code": "DANDYA-001", "title": self.DANDY_TITLE, "actress": "大浦真奈美", "score": 0.9},
                {"code": "DANDY-900", "title": self.DANDY_TITLE, "actress": "大浦真奈美", "score": 0.8},
            ],
        }
        with contextlib.ExitStack() as stack:
            for p in self._pipeline_mocks(search_hit=hit):
                stack.enter_context(p)
            result, status = S.run_identify_pipeline(user_title=self.DANDY_TITLE)
        self.assertEqual(status, 200)
        self.assertEqual(result.get("search_mode"), "title")
        self._assert_dandya_buckets(result)

    def test_unresolved_title_still_stamps_keywords_and_keyword_bucket(self):
        with contextlib.ExitStack() as stack:
            for p in self._pipeline_mocks(search_hit={"title": self.DANDY_TITLE, "candidates": []}):
                stack.enter_context(p)
            result, status = S.run_identify_pipeline(user_title=self.DANDY_TITLE)
        self.assertEqual(status, 200)
        self.assertEqual(result.get("theme_keywords"), self.EXPECTED_KEYWORDS)
        rel = result.get("related_by_title") or []
        self.assertTrue(any(r.get("line") == "keyword" for r in rel), rel)
        self.assertLessEqual(sum(1 for r in rel if r.get("line") == "keyword"), 5)

    def test_keyword_bucket_runs_after_title_budget_is_spent(self):
        seen = {}

        def fake_keywords(*args, **kwargs):
            seen["budget"] = kwargs.get("budget_sec")
            return self._keyword_rows(6)

        with contextlib.ExitStack() as stack:
            for p in self._pipeline_mocks(search_hit=None, keyword_rows=[]):
                stack.enter_context(p)
            stack.enter_context(
                mock.patch.object(S, "_find_related_by_keywords", side_effect=fake_keywords)
            )
            out = S.find_related_by_title(
                self.DANDY_TITLE,
                exclude_code="DANDYA-001",
                actress="大浦真奈美",
                budget_sec=0.01,
            )
        self.assertGreaterEqual(float(seen.get("budget") or 0), S.RELATED_KEYWORD_BUDGET)
        kw = [r for r in out if r.get("line") == "keyword"]
        self.assertGreaterEqual(len(kw), 1, out)
        self.assertLessEqual(len(kw), 5)
        self.assertLessEqual(sum(1 for r in out if r.get("line") == "actress"), 3)

    def test_image_path_keeps_visual_lock_and_keyword_bucket(self):
        img = b"user-image-dandya"
        calls = {"n": 0}

        def fake_rank(user, cands, api_key=None, **kwargs):
            calls["n"] += 1
            self.assertEqual(user, img)
            self.assertTrue(cands)
            best = dict(cands[0])
            best["visual"] = self._lock_vm()
            best["visual_score"] = 0.91
            return [best], {
                "visual_ranked": True,
                "note": "已對照使用者原圖（視覺鎖定）",
                "note_stills": True,
                "compared": 2,
                "visual_lock": True,
            }

        hit = {
            "code": "DANDYA-001",
            "title": self.DANDY_TITLE,
            "actress": "大浦真奈美",
            "score": 0.4,
            "candidates": [],
        }
        with contextlib.ExitStack() as stack:
            for p in self._pipeline_mocks(search_hit=hit):
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(S, "get_gemini_api_key", return_value="test-key"))
            stack.enter_context(
                mock.patch.object(S, "call_gemini_vision", return_value={"code": None, "title": None})
            )
            stack.enter_context(mock.patch.object(S, "rank_candidates_by_visual", side_effect=fake_rank))
            result, status = S.run_identify_pipeline(image_bytes=img, user_code="DANDYA-001")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(calls["n"], 1)
        blob = (result.get("message") or "") + (result.get("related_note") or "")
        self.assertIn("視覺鎖定", blob)
        self._assert_dandya_buckets(result)

    def _lock_vm(self):
        return {
            "same_work": True,
            "confidence": 0.93,
            "reason": "same crop",
            "match_person": True,
            "match_face": True,
            "match_accessories": True,
            "match_clothes": True,
            "match_pose": True,
        }

    def _reject_vm(self):
        return {
            "same_work": False,
            "confidence": 0.22,
            "reason": "clothes differ",
            "match_person": True,
            "match_face": False,
            "match_accessories": False,
            "match_clothes": False,
            "match_pose": False,
        }

    def _rank(self, candidates, responses):
        calls = {"n": 0}

        def fake_dl(url, timeout=None):
            u = str(url)
            if "cover" in u:
                return b"cover"
            if "still-1" in u:
                return b"s1"
            if "still-2" in u:
                return b"s2"
            return None

        def fake_gemini(user, covers, key, timeout=10.0, labels=None):
            idx = calls["n"]
            calls["n"] += 1
            row = responses[min(idx, len(responses) - 1)]
            if isinstance(row, list):
                self.assertEqual(len(row), len(covers))
                return row
            return [row]

        with mock.patch.object(S, "download_cover_bytes", side_effect=fake_dl), mock.patch.object(
            S, "gemini_rank_covers_batch", side_effect=fake_gemini
        ):
            return S.rank_candidates_by_visual(
                b"user-image",
                candidates,
                api_key="test-key",
                budget_s=5,
            )

    def test_cover_lock_survives_one_disagreeing_still(self):
        cand = {
            "code": "DANDY-893",
            "title": self.DANDY_TITLE,
            "score": 0.8,
            "cover": "https://example.com/cover.jpg",
            "stills": ["https://example.com/still-1.jpg"],
        }
        ranked, meta = self._rank([cand], [self._lock_vm(), self._reject_vm()])
        self.assertTrue(meta.get("visual_ranked"))
        self.assertTrue(meta.get("note_stills"))
        self.assertIn("視覺鎖定", meta.get("note") or "")
        self.assertNotIn("已否決封面誤判", meta.get("note") or "")
        self.assertNotIn("僅排序未鎖定", meta.get("note") or "")
        self.assertTrue((ranked[0].get("visual") or {}).get("same_work"))
        self.assertTrue((ranked[0].get("visual") or {}).get("match_clothes"))

    def test_two_rejecting_stills_revoke_and_document_sort_only(self):
        cand = {
            "code": "DANDY-893",
            "title": self.DANDY_TITLE,
            "score": 0.8,
            "cover": "https://example.com/cover.jpg",
            "stills": [
                "https://example.com/still-1.jpg",
                "https://example.com/still-2.jpg",
            ],
        }
        ranked, meta = self._rank(
            [cand],
            [self._lock_vm(), [self._reject_vm(), self._reject_vm()]],
        )
        note = meta.get("note") or ""
        self.assertIn("僅排序未鎖定", note)
        self.assertIn("已否決封面誤判", note)
        self.assertIn("無法視覺鎖定", note)
        self.assertNotIn("；視覺鎖定；", note)
        self.assertTrue(meta.get("stills_revoked"))
        self.assertFalse((ranked[0].get("visual") or {}).get("same_work"))

    def test_still_lock_when_cover_does_not(self):
        cand = {
            "code": "DANDY-893",
            "title": self.DANDY_TITLE,
            "score": 0.4,
            "cover": "https://example.com/cover.jpg",
            "stills": ["https://example.com/still-1.jpg"],
        }
        ranked, meta = self._rank([cand], [self._reject_vm(), self._lock_vm()])
        self.assertIn("視覺鎖定", meta.get("note") or "")
        self.assertTrue((ranked[0].get("visual") or {}).get("same_work"))
        self.assertTrue((ranked[0].get("visual") or {}).get("match_clothes"))

    def test_lock_outranks_higher_title_score(self):
        cands = [
            {
                "code": "AAA-001",
                "title": "全然違うタイトルですよ",
                "score": 0.99,
                "cover": "https://example.com/cover-a.jpg",
            },
            {
                "code": "AAA-002",
                "title": "もう一つの作品タイトル",
                "score": 0.12,
                "cover": "https://example.com/cover-b.jpg",
            },
        ]
        ranked, meta = self._rank(cands, [[self._reject_vm(), self._lock_vm()]])
        self.assertEqual(ranked[0]["code"], "AAA-002")
        self.assertIn("視覺鎖定", meta.get("note") or "")
        self.assertIn("AAA-002", meta.get("note") or "")

    def test_image_rescan_rechecks_instead_of_replaying_cache(self):
        img = b"user-image-bytes-dandy"
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DANDY-893",
                "title": self.DANDY_TITLE,
                "title_zh": "中文主標",
                "actress": "竹内夏希",
                "cover": "https://example.com/c.jpg",
                "stills": ["https://example.com/s.jpg"],
                "message": "舊的僅排序未鎖定",
            },
            image_hash=S.image_content_hash(img),
        )

        def fake_rank(user, cands, api_key=None, **kwargs):
            self.assertEqual(user, img)
            self.assertTrue(any(c.get("code") == "DANDY-893" for c in cands))
            best = dict(cands[0])
            best["visual"] = self._lock_vm()
            best["visual_score"] = 0.9
            note = (
                "已對照使用者原圖比對 2 張封面＋劇照"
                "（主選 DANDY-893；視覺鎖定；依人物／衣服／表情／飾品／姿勢）"
            )
            return [best], {
                "visual_ranked": True,
                "note": note,
                "note_stills": True,
                "compared": 2,
                "visual_lock": True,
            }

        with mock.patch.object(S, "rank_candidates_by_visual", side_effect=fake_rank), mock.patch.object(
            S, "get_gemini_api_key", return_value="test-key"
        ), mock.patch.object(S, "find_related_by_title", return_value=[]), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(S, "fetch_avbase_by_code", return_value=None):
            result, status = S.run_identify_pipeline(image_bytes=img)
        self.assertEqual(status, 200)
        self.assertTrue(result.get("image_reverified"))
        self.assertIn("視覺鎖定", result.get("message") or "")
        self.assertEqual(result.get("theme_keywords"), self.EXPECTED_KEYWORDS)

    def test_single_unlocked_cache_does_not_skip_a_fresh_search(self):
        img = b"user-image-bytes-unlocked"
        S.offline_cache_put(
            {
                "ok": True,
                "code": "DANDY-893",
                "title": self.DANDY_TITLE,
                "title_zh": "中文主標",
                "actress": "竹内夏希",
                "cover": "https://example.com/c.jpg",
                "message": "僅排序未鎖定",
            },
            image_hash=S.image_content_hash(img),
        )

        def fake_rank(user, cands, api_key=None, **kwargs):
            item = dict(cands[0])
            item["visual"] = self._reject_vm()
            return [item], {
                "visual_ranked": True,
                "note": "僅排序未鎖定（封面與劇照皆未同時符合同一作品與衣服）",
                "note_stills": True,
                "compared": 2,
            }

        with mock.patch.object(S, "rank_candidates_by_visual", side_effect=fake_rank), mock.patch.object(
            S, "get_gemini_api_key", return_value="test-key"
        ), mock.patch.object(S, "find_related_by_title", return_value=[]), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(S, "fetch_avbase_by_code", return_value=None), mock.patch.object(
            S, "call_gemini_vision", return_value={"title": None, "code": None}
        ), mock.patch.object(S, "ocr_image_bytes", return_value=""):
            result, status = S.run_identify_pipeline(image_bytes=img)
        self.assertFalse(result.get("image_reverified"))
        self.assertNotEqual(result.get("code"), "DANDY-893")


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
