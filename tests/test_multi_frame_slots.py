#!/usr/bin/env python3
"""Multi-image identify keeps one slot per upload and resolves title hits.

The failure mode is general: N frames must not collapse to fewer works just
because one read failed or two titles shared a token, and a title the catalog
can search must come back with a code and cover. Product ids here are fictional.
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


def _lock_visual(take: bool) -> dict:
    return {
        "same_work": take,
        "confidence": 0.93 if take else 0.12,
        "match_person": take,
        "match_face": take,
        "match_accessories": take,
        "match_clothes": take,
        "match_pose": take,
    }


class TestCatalogTitleQueries(unittest.TestCase):
    def test_glued_actress_is_not_the_only_query(self):
        for raw in ("星屑が詩 誤読花子", "星屑が詩誤読花子", "誤読花子星屑が詩"):
            queries = S._catalog_title_queries(raw, "誤読花子")
            self.assertTrue(queries, raw)
            self.assertEqual(queries[0], "星屑が詩", (raw, queries))
            self.assertNotIn("誤読花子", queries[0])

    def test_raw_title_is_searched_when_variants_explode(self):
        if not hasattr(S, "title_query_variants"):
            S.title_query_variants = lambda title: [title]

        seen = []

        def fake_fetch(q, actress=None):
            seen.append((q, actress))
            if q == "星屑が詩":
                return [
                    {
                        "code": "TEST-222",
                        "title": "星屑が詩の全記録",
                        "actress": "本当花子",
                        "studio": "テスト",
                        "cid": "test00222",
                        "cover": "https://example.com/star.jpg",
                        "source": "avbase",
                        "score": 0.92,
                    }
                ]
            return []

        with mock.patch.object(S, "title_query_variants", side_effect=RuntimeError("variants down")), mock.patch.object(
            S, "fetch_avbase_title_results", side_effect=fake_fetch
        ), mock.patch.object(S, "probe_cover_url", side_effect=lambda url, timeout=0: (True, url)):
            hit = S.search_by_title("星屑が詩 誤読花子", actress="誤読花子")

        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("code"), "TEST-222")
        self.assertEqual(hit.get("actress"), "本当花子")
        self.assertTrue(seen, seen)
        self.assertEqual(seen[0][0], "星屑が詩")
        self.assertIsNone(seen[0][1])
        self.assertTrue(all(actress is None for _q, actress in seen), seen)


class TestMultiFrameSlots(unittest.TestCase):
    """Three-frame multi identify: code-rich, title-only, still-only."""

    def _rank(self, img_code, img_title, calls):
        def fake_rank(user, cands, api_key=None, **kwargs):
            calls.append({"image": user, "codes": [c.get("code") for c in (cands or [])]})
            want = None
            if user == img_code:
                want = "TEST-111"
            elif user == img_title:
                want = "TEST-222"
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                take = bool(want) and str(item.get("code") or "") == want
                item["visual"] = _lock_visual(take)
                item["visual_score"] = 0.93 if take else 0.12
                ranked.append(item)
            ranked.sort(
                key=lambda it: (
                    1 if (it.get("visual") or {}).get("same_work") else 0,
                    float(it.get("visual_score") or 0),
                ),
                reverse=True,
            )
            best = (ranked[0].get("visual") if ranked else {}) or {}
            locked = bool(best.get("same_work") and best.get("match_clothes"))
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": locked,
                "compared": len(ranked),
                "note": "locked" if locked else "open",
            }

        return fake_rank

    def test_three_frames_keep_slots_and_title_resolves(self):
        img_code = b"code-rich-frame"
        img_title = b"title-rich-frame"
        img_still = b"still-only-frame"
        searches = []
        rank_calls = []

        def fake_vision(image_bytes, mime, api_key):
            if image_bytes == img_code:
                return {"code": "TEST-111", "title": "水泳部の合宿", "actress": "青葉"}
            if image_bytes == img_title:
                return {"title": "星屑が詩", "actress": "誤読花子"}
            return {}

        def fake_pipe(**kwargs):
            self.assertIsNone(kwargs.get("image_bytes"))
            code = str(kwargs.get("user_code") or "")
            title = str(kwargs.get("user_title") or "")
            if code == "TEST-111":
                return {
                    "ok": True,
                    "code": "TEST-111",
                    "title": "水泳部の合宿記録",
                    "actress": "青葉",
                    "studio": "テスト",
                    "cover": "https://example.com/swim.jpg",
                    "stills": ["https://example.com/swim-s.jpg"],
                    "cid": "test00111",
                    "candidates": [
                        {
                            "code": "TEST-111",
                            "title": "水泳部の合宿記録",
                            "actress": "青葉",
                            "cover": "https://example.com/swim.jpg",
                            "stills": ["https://example.com/swim-s.jpg"],
                        }
                    ],
                }, 200
            if "星屑" in title:
                # First title pass misses. Escalation must search the catalog anyway.
                return S.empty_identify(message="找不到片名", title=title, search_mode="title"), 200
            raise AssertionError(kwargs)

        catalog = {
            "ok": True,
            "code": "TEST-222",
            "title": "星屑が詩の全記録",
            "actress": "本当花子",
            "studio": "テスト",
            "cover": "https://example.com/star.jpg",
            "stills": ["https://example.com/star-s.jpg"],
            "cid": "test00222",
            "source": "avbase",
            "score": 0.92,
        }

        def fake_search(title, actress=None):
            searches.append((str(title or ""), actress))
            if "星屑が詩" in str(title or ""):
                hit = dict(catalog)
                hit["candidates"] = [dict(catalog)]
                return hit
            return None

        def fake_identify(code, ocr_preview=None, vision_meta=None):
            self.assertEqual(S.format_display_code(str(code)), "TEST-222")
            return dict(catalog)

        def fake_attach(result, **kwargs):
            if isinstance(result, dict) and not result.get("unidentified"):
                S._recompute_theme_keywords(result)
            if isinstance(result, dict):
                result.setdefault("related_by_title", [])
            return result

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(S, "ocr_image_bytes", return_value=""), mock.patch.object(
            S, "offline_cache_get", return_value=None
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_pipe
        ), mock.patch.object(
            S, "search_by_title", side_effect=fake_search
        ), mock.patch.object(
            S, "identify_code", side_effect=fake_identify
        ), mock.patch.object(
            S, "rank_candidates_by_visual", side_effect=self._rank(img_code, img_title, rank_calls)
        ), mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach):
            payload, status = S.run_multi_identify_pipeline(
                [(img_code, "a.jpg"), (img_title, "b.jpg"), (img_still, "c.jpg")]
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("image_count"), 3)
        results = payload.get("results") or []
        self.assertEqual(len(results), 3, payload.get("related_note"))
        note = str(payload.get("related_note") or "") + str(payload.get("message") or "")
        self.assertNotIn("略過", note)
        self.assertNotIn("去重", note)
        self.assertNotIn("未讀到", note)

        coded = results[0]
        self.assertEqual(coded.get("code"), "TEST-111")
        self.assertEqual(coded.get("from_image_index"), 1)
        self.assertTrue(coded.get("cover"))
        self.assertFalse(coded.get("unidentified"))

        titled = results[1]
        self.assertEqual(titled.get("code"), "TEST-222")
        self.assertEqual(titled.get("from_image_index"), 2)
        self.assertEqual(titled.get("title"), "星屑が詩の全記録")
        self.assertEqual(titled.get("actress"), "本当花子")
        self.assertNotEqual(titled.get("actress"), "誤読花子")
        self.assertTrue(titled.get("cover"))
        self.assertTrue(titled.get("stills"))
        self.assertFalse(titled.get("needs_code"))
        self.assertFalse(titled.get("stub"))

        still = results[2]
        self.assertEqual(still.get("from_image_index"), 3)
        self.assertTrue(still.get("unidentified"))
        self.assertNotEqual(still.get("code"), "TEST-111")
        self.assertIn("未辨識", note)

        self.assertTrue(searches, searches)
        self.assertTrue(all(actress is None for _q, actress in searches), searches)
        self.assertTrue(any("星屑が詩" in q and "誤読" not in q for q, _a in searches), searches)
        title_ranks = [c for c in rank_calls if c["image"] == img_title]
        self.assertTrue(title_ranks, rank_calls)
        self.assertTrue(any("TEST-222" in (c["codes"] or []) for c in title_ranks), title_ranks)

    def test_same_code_without_both_locks_is_not_dropped(self):
        img_a = b"frame-a"
        img_b = b"frame-b"

        def fake_vision(image_bytes, mime, api_key):
            if image_bytes == img_a:
                return {"title": "第一張作品標題"}
            return {"title": "第二張作品標題"}

        def fake_pipe(**kwargs):
            return {
                "ok": True,
                "code": "TEST-111",
                "title": "水泳部の合宿記録",
                "actress": "青葉",
                "cover": "https://example.com/swim.jpg",
                "stills": ["https://example.com/swim-s.jpg"],
                "candidates": [
                    {
                        "code": "TEST-111",
                        "title": "水泳部の合宿記録",
                        "cover": "https://example.com/swim.jpg",
                    }
                ],
            }, 200

        def fake_rank(user, cands, api_key=None, **kwargs):
            take = user == img_a
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                item["visual"] = _lock_visual(take and str(item.get("code") or "") == "TEST-111")
                item["visual_score"] = 0.93 if item["visual"]["same_work"] else 0.1
                ranked.append(item)
            best = (ranked[0].get("visual") if ranked else {}) or {}
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": bool(best.get("same_work") and best.get("match_clothes")),
                "compared": len(ranked),
                "note": "",
            }

        def fake_attach(result, **kwargs):
            if isinstance(result, dict):
                result.setdefault("related_by_title", [])
            return result

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(S, "run_identify_pipeline", side_effect=fake_pipe), mock.patch.object(
            S, "rank_candidates_by_visual", side_effect=fake_rank
        ), mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach), mock.patch.object(
            S, "search_by_title", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline([(img_a, "a.jpg"), (img_b, "b.jpg")])

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2, payload.get("related_note"))
        self.assertEqual([r.get("from_image_index") for r in results], [1, 2])
        note = str(payload.get("related_note") or "")
        self.assertNotIn("略過", note)
        self.assertNotIn("去重", note)

    def test_both_visual_locks_may_merge_same_code(self):
        img_a = b"frame-a"
        img_b = b"frame-b"

        def fake_vision(image_bytes, mime, api_key):
            return {"title": "同一作品的不同截圖" + ("甲" if image_bytes == img_a else "乙")}

        def fake_pipe(**kwargs):
            return {
                "ok": True,
                "code": "TEST-111",
                "title": "水泳部の合宿記録",
                "actress": "青葉",
                "cover": "https://example.com/swim.jpg",
                "stills": ["https://example.com/swim-s.jpg"],
                "candidates": [
                    {
                        "code": "TEST-111",
                        "title": "水泳部の合宿記録",
                        "cover": "https://example.com/swim.jpg",
                    }
                ],
            }, 200

        def fake_rank(user, cands, api_key=None, **kwargs):
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                take = str(item.get("code") or "") == "TEST-111"
                item["visual"] = _lock_visual(take)
                item["visual_score"] = 0.95 if take else 0.1
                ranked.append(item)
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": True,
                "compared": len(ranked),
                "note": "locked",
            }

        def fake_attach(result, **kwargs):
            if isinstance(result, dict):
                result.setdefault("related_by_title", [])
            return result

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(S, "run_identify_pipeline", side_effect=fake_pipe), mock.patch.object(
            S, "rank_candidates_by_visual", side_effect=fake_rank
        ), mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach), mock.patch.object(
            S, "search_by_title", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline([(img_a, "a.jpg"), (img_b, "b.jpg")])

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 1, payload.get("related_note"))
        self.assertEqual(results[0].get("code"), "TEST-111")
        self.assertEqual(results[0].get("merged_image_indexes"), [1, 2])
        note = str(payload.get("related_note") or "")
        self.assertIn("合併", note)
        self.assertNotIn("略過", note)
        self.assertNotIn("未讀到", note)


class TestSwimsuitCoverStaysBesideJufeAndTitle(unittest.TestCase):
    """The dropped card was the school-swimsuit cover, not the yellow-bikini crop.

    Left: a title-rich 媚薬／水泳部 cover that identifies on its own.
    Middle: a short phrase (舌技が神) that must gain a code and catalog title.
    Right: a still crop that resolves like JUFE-271, and must not absorb the left cover.
    """

    SWIM_TITLE = "巨乳水泳部員の媚薬合宿記録"
    SHJG_TITLE = "舌技が神と呼ばれる架空の夜"
    JULX_TITLE = "地味な眼鏡では隠し切れない美人OLの完全生撮り"

    def _payload(self, code, title, actress, cover):
        hit = {
            "ok": True,
            "code": code,
            "title": title,
            "actress": actress,
            "studio": "架空",
            "cover": cover,
            "stills": [cover + "-s.jpg"],
            "cid": code.lower().replace("-", "") + "cid",
            "source": "avbase",
            "score": 0.94,
        }
        hit["candidates"] = [dict(hit)]
        return hit

    def _search(self, title, actress=None):
        text = str(title or "")
        if "媚薬" in text or "水泳部" in text:
            return self._payload("SWIM-804", self.SWIM_TITLE, "架空泳子", "https://example.com/swim804.jpg")
        if "舌技" in text:
            return self._payload("SHJG-448", self.SHJG_TITLE, "架空ゆうき", "https://example.com/shjg448.jpg")
        return None

    def _identify(self, code, ocr_preview=None, vision_meta=None):
        disp = S.format_display_code(str(code))
        if disp == "SWIM-804":
            return self._payload("SWIM-804", self.SWIM_TITLE, "架空泳子", "https://example.com/swim804.jpg")
        if disp == "SHJG-448":
            return self._payload("SHJG-448", self.SHJG_TITLE, "架空ゆうき", "https://example.com/shjg448.jpg")
        if disp == "JULX-271":
            return self._payload("JULX-271", self.JULX_TITLE, "架空カレン", "https://example.com/julx271.jpg")
        return {"ok": False, "code": disp, "title": None, "cover": None}

    def _rank_for(self, expected):
        def fake_rank(user, cands, api_key=None, **kwargs):
            want = expected.get(user)
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                code = S.format_display_code(str(item.get("code") or ""))
                take = bool(want) and code == want
                item["visual"] = _lock_visual(take)
                item["visual_score"] = 0.93 if take else 0.1
                ranked.append(item)
            ranked.sort(key=lambda it: float(it.get("visual_score") or 0), reverse=True)
            best = (ranked[0].get("visual") if ranked else {}) or {}
            locked = bool(best.get("same_work") and best.get("match_clothes"))
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": locked,
                "compared": len(ranked),
                "note": "locked" if locked else "open",
            }

        return fake_rank

    def _patches(self, vision, ocr, rank, cache):
        return mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=self._search),
            identify_code=mock.Mock(side_effect=self._identify),
            rank_candidates_by_visual=mock.Mock(side_effect=rank),
            offline_cache_get=mock.Mock(side_effect=cache),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        )

    def test_title_rich_cover_keeps_its_code_beside_phrase_and_crop(self):
        img_swim = b"school-swimsuit-cover"
        img_phrase = b"tongue-title"
        img_crop = b"yellow-bikini-crop"
        expected = {img_swim: "SWIM-804", img_phrase: "SHJG-448", img_crop: "JULX-271"}

        def vision(image_bytes, mime, api_key):
            if image_bytes == img_phrase:
                return {"title": "舌技が神", "actress": "柊ゆうき"}
            if image_bytes == img_crop:
                return {"code": "JULX-271"}
            return {}

        def ocr(image_bytes):
            if image_bytes == img_swim:
                return "巨乳水泳部員の媚薬合宿記録"
            return ""

        images = [(img_swim, "swim.jpg"), (img_phrase, "phrase.jpg"), (img_crop, "crop.jpg")]
        with self._patches(vision, ocr, self._rank_for(expected), lambda **kwargs: None):
            alone, alone_status = S.run_identify_pipeline(image_bytes=img_swim, filename="swim.jpg")
            payload, status = S.run_multi_identify_pipeline(images)

        self.assertEqual(alone_status, 200)
        self.assertEqual(alone.get("code"), "SWIM-804")
        self.assertEqual(alone.get("title"), self.SWIM_TITLE)
        self.assertTrue(alone.get("cover"))

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("image_count"), 3)
        self.assertEqual(payload.get("dropped"), [])
        results = payload.get("results") or []
        self.assertEqual(len(results), 3, payload.get("related_note"))
        by_index = {row.get("from_image_index"): row for row in results}
        self.assertEqual(set(by_index), {1, 2, 3})
        self.assertEqual(by_index[1].get("code"), "SWIM-804")
        self.assertEqual(by_index[1].get("title"), self.SWIM_TITLE)
        self.assertTrue(by_index[1].get("cover"))
        self.assertEqual(by_index[2].get("code"), "SHJG-448")
        self.assertEqual(by_index[2].get("title"), self.SHJG_TITLE)
        self.assertNotEqual(by_index[2].get("actress"), "柊ゆうき")
        self.assertTrue(by_index[2].get("cover"))
        self.assertEqual(by_index[3].get("code"), "JULX-271")
        self.assertEqual(by_index[3].get("title"), self.JULX_TITLE)
        note = (payload.get("related_note") or "") + (payload.get("message") or "")
        self.assertNotIn("略過", note)
        self.assertNotIn("去重", note)
        self.assertNotIn("合併", note)

    def test_misread_shared_code_does_not_swallow_the_swimsuit_title(self):
        img_swim = b"school-swimsuit-cover"
        img_phrase = b"tongue-title"
        img_crop = b"yellow-bikini-crop"
        expected = {img_swim: "SWIM-804", img_phrase: "SHJG-448", img_crop: "JULX-271"}

        def vision(image_bytes, mime, api_key):
            if image_bytes == img_swim:
                return {"code": "JULX-271", "title": "巨乳水泳部員の媚薬合宿記録"}
            if image_bytes == img_phrase:
                return {"title": "舌技が神", "actress": "柊ゆうき"}
            return {"code": "JULX-271"}

        with self._patches(vision, lambda image_bytes: "", self._rank_for(expected), lambda **kwargs: None):
            payload, status = S.run_multi_identify_pipeline(
                [(img_swim, "swim.jpg"), (img_phrase, "phrase.jpg"), (img_crop, "crop.jpg")]
            )

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 3, payload.get("related_note"))
        codes = [row.get("code") for row in results]
        self.assertEqual(codes, ["SWIM-804", "SHJG-448", "JULX-271"])
        self.assertEqual(results[0].get("title"), self.SWIM_TITLE)
        self.assertNotIn("合併", payload.get("related_note") or "")

    def test_previous_success_cache_restores_swimsuit_without_merging(self):
        img_swim = b"school-swimsuit-cover"
        img_phrase = b"tongue-title"
        img_crop = b"yellow-bikini-crop"
        swim_hash = S.image_content_hash(img_swim)
        expected = {img_swim: "SWIM-804", img_phrase: "SHJG-448", img_crop: "JULX-271"}
        cached = self._payload("SWIM-804", self.SWIM_TITLE, "架空泳子", "https://example.com/swim804.jpg")

        def vision(image_bytes, mime, api_key):
            if image_bytes == img_phrase:
                return {"title": "舌技が神", "actress": "柊ゆうき"}
            if image_bytes == img_crop:
                return {"code": "JULX-271"}
            return {}

        def cache(*, code=None, image_hash=None):
            if image_hash and image_hash == swim_hash:
                return dict(cached)
            return None

        with self._patches(vision, lambda image_bytes: "", self._rank_for(expected), cache):
            payload, status = S.run_multi_identify_pipeline(
                [(img_swim, "swim.jpg"), (img_phrase, "phrase.jpg"), (img_crop, "crop.jpg")]
            )

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 3, payload.get("related_note"))
        self.assertEqual(results[0].get("code"), "SWIM-804")
        self.assertEqual(results[0].get("title"), self.SWIM_TITLE)
        self.assertEqual(results[0].get("from_image_index"), 1)
        self.assertTrue(results[0].get("from_offline_cache"))
        self.assertEqual(results[2].get("code"), "JULX-271")
        self.assertNotIn("合併", payload.get("related_note") or "")


if __name__ == "__main__":
    unittest.main()
