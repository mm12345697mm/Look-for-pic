#!/usr/bin/env python3
"""APGH-012: the real shape behind a 「舌技が神」 title-only card.

The catalog title is 先生が2人っきりのプライベート補習で全部面倒みてあげる.
It does not contain 舌技. Title search on the cover slogan must not be treated
as success, and must not invent a 舌技が神 catalog title.

Two ways this frame resolves:
- the listing caption prints one 品番 (APGH-012 / ApGH-012)
- no 品番, but actress 柊ゆうき plus a visual lock on that cover

Full-text search (every printed line, not just the slogan) runs only when the
shot is one complete cover AND the read has no listing row under the thumb.
A grid, multi-row screenshot, or player overlay keeps the focused code and
primary title. Do not drop this gate.

SHJG-448 in other tests is a fictional phrase-in-title mechanic, not this work.
"""
from __future__ import annotations

import re
import unittest
from unittest import mock

import server as S


APGH_TITLE = "先生が2人っきりのプライベート補習で全部面倒みてあげる"
APGH_CODE = "APGH-012"
ACTRESS = "柊ゆうき"


def _lock(take: bool) -> dict:
    return {
        "same_work": take,
        "confidence": 0.91 if take else 0.12,
        "match_person": take,
        "match_face": take,
        "match_accessories": take,
        "match_clothes": take,
        "match_pose": take,
        "reason": "lock" if take else "different clothes",
    }


def _work(code: str, title: str) -> dict:
    hit = {
        "ok": True,
        "code": code,
        "title": title,
        "actress": ACTRESS,
        "studio": "オーロラ",
        "cover": "https://example.com/" + code.lower() + ".jpg",
        "stills": [],
        "cid": code.lower().replace("-", "") + "cid",
        "source": "avbase",
        "score": 0.72,
    }
    hit["candidates"] = [dict(hit)]
    return hit


WORKS = {
    "APGH-012": _work("APGH-012", APGH_TITLE),
    "APGH-015": _work("APGH-015", "別作品の家庭教師は今日も居残り"),
    "APGH-022": _work("APGH-022", "別作品の職員室で終わらない面談"),
}


class TestApgh012Overlay(unittest.TestCase):
    def test_ocr_noise_is_not_a_product_code(self):
        self.assertEqual(S._trusted_ocr_codes("rake 12\nshat          676\nyr 33"), [])
        self.assertEqual(S._sole_trusted_ocr_code("APGH-012\n舌技が神")[0], "APGH-012")
        self.assertIsNone(S._sole_trusted_ocr_code("APGH-012\nAPGH-015")[0])

    def test_series_jacket_lock_beats_a_short_unique_phrase(self):
        """No 品番. A short unique hit must not beat a jacket-locked series volume."""
        series_title = "架空シリーズの長い共通タイトルで巻だけが違う作品群"
        volumes = [
            _work("SER-001", series_title),
            _work("SER-002", series_title),
            _work("SER-003", series_title),
        ]
        slogan = _work("SLG-009", "短い標語だけの別作品")

        def search(title, actress=None):
            q = re.sub(r"\s+", "", str(title or ""))
            if q == "共通題名":
                hit = dict(volumes[0])
                hit["candidates"] = [dict(v) for v in volumes]
                return hit
            if "標語" in q or q == "短い標語":
                hit = dict(slogan)
                hit["candidates"] = [dict(slogan)]
                return hit
            return None

        def lock(image, cands):
            for cand in cands or []:
                if S.format_display_code(str(cand.get("code") or "")) == "SER-002":
                    winner = dict(cand)
                    winner["jacket_score"] = 0.91
                    return winner
            return None

        resolved = None
        with mock.patch.multiple(
            S,
            search_by_title=mock.Mock(side_effect=search),
            _jacket_lock_winner=mock.Mock(side_effect=lock),
        ):
            resolved = S._resolve_unnumbered_cover(
                ["短い標語", "共通題名"],
                b"cover-bytes",
            )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("code"), "SER-002")
        self.assertTrue((resolved.get("visual_meta") or {}).get("visual_lock"))

    def test_jacket_match_keeps_code_when_ocr_title_disagrees(self):
        meta = {
            "ok": False,
            "cover_ok": True,
            "cover": "https://example.com/ser-002.jpg",
            "title_ok": False,
            "reason": "片名不符(sim=0.10)",
        }
        with mock.patch.object(S, "_jacket_score_against_url", return_value=0.91):
            keep, score = S._ocr_code_survives_title_mismatch(meta, b"img")
        self.assertTrue(keep)
        self.assertGreaterEqual(score, 0.70)
        with mock.patch.object(S, "_jacket_score_against_url", return_value=0.22):
            keep_low, _score = S._ocr_code_survives_title_mismatch(meta, b"img")
        self.assertFalse(keep_low)

    def test_glued_runtime_is_a_code_candidate(self):
        cands = S._ocr_code_candidates("作品 ABCD-100240分\nrake 12")
        self.assertIn("ABCD-100", cands)
        self.assertNotIn("ABCD-100240", cands)
        self.assertEqual(S._trusted_ocr_codes("rake 12"), [])

    def test_confusion_variants_include_letter_and_digit_neighbor(self):
        variants = S._confusion_variants("QLQ-621")
        self.assertIn("QUQ-624", variants)

    def test_jacket_pick_prefers_the_matching_cover(self):
        def cover(code):
            if code == "GOOD-100":
                return "cid", "https://example.com/good.jpg"
            return "cid", "https://example.com/bad.jpg"

        def score(image, url):
            return 0.93 if "good" in str(url) else 0.21

        with mock.patch.object(S, "resolve_cover_cid", side_effect=cover):
            with mock.patch.object(S, "_jacket_score_against_url", side_effect=score):
                picked, best, compared = S._pick_code_by_jacket(
                    ["BAD-621", "GOOD-100"], b"img"
                )
        self.assertTrue(compared)
        self.assertEqual(picked, "GOOD-100")
        self.assertGreaterEqual(best, 0.70)

    def test_prefix_queries_skip_symbol_smears(self):
        blob = "xyz !! をな属人金欲にい ff\n短い標語です？！"
        prefixes = S._ocr_line_prefixes(blob)
        self.assertTrue(any(p.startswith("短い標語") for p in prefixes), prefixes)
        self.assertFalse(any("をな属" in p for p in prefixes), prefixes)

    def test_official_title_chips_are_compounds_not_slices(self):
        chips = S._extract_title_theme_keywords(APGH_TITLE, actress=ACTRESS)
        blob = " ".join(chips)
        for bad in ("人っきりのプ", "人っきりりプ", "ライベート補", "習で全部面倒"):
            self.assertNotIn(bad, chips, blob)
        for good in ("先生", "2人っきり", "プライベート補習", "面倒みてあげる"):
            self.assertIn(good, chips, blob)

    def test_manual_code_pins_slogan_frame_not_a_resolved_neighbor(self):
        rows = [
            {"code": "JUFE-271", "title": "地味な眼鏡では隠し切れない美人OL"},
            {"code": None, "title": "舌技が神"},
            {"code": None, "title": "根スケベ妻と精飲"},
        ]
        self.assertTrue(S._pin_manual_query(rows, "apgh-012", ""))
        self.assertEqual(rows[0]["code"], "JUFE-271")
        self.assertEqual(rows[1]["code"], "APGH-012")
        self.assertIsNone(rows[2]["code"])
        self.assertTrue(S._pin_manual_query(rows, "APGH-012", ""))


    def _identify(self, code, ocr_preview=None, vision_meta=None):
        disp = S.format_display_code(str(code))
        row = WORKS.get(disp)
        if not row:
            return {"ok": False, "code": disp, "title": None, "cover": None}
        return _work(disp, row["title"])

    def _fetch(self, title, actress=None):
        q = S.format_display_code(str(title or "")) if S.parse_code_parts(str(title or "")) else ""
        if q in WORKS:
            return [_work(q, WORKS[q]["title"])]
        name = str(actress or title or "")
        if "柊" in name:
            return [_work(code, WORKS[code]["title"]) for code in ("APGH-022", "APGH-015", "APGH-012")]
        return []

    def _rank(self, locked_code):
        def fake(user, cands, api_key=None, **kwargs):
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                code = S.format_display_code(str(item.get("code") or ""))
                take = bool(locked_code) and code == locked_code
                item["visual"] = _lock(take)
                item["visual_score"] = 0.93 if take else 0.08
                ranked.append(item)
            ranked.sort(key=lambda it: float(it.get("visual_score") or 0), reverse=True)
            best = (ranked[0].get("visual") if ranked else {}) or {}
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": bool(best.get("same_work") and best.get("match_clothes")),
                "compared": len(ranked),
                "note": "locked" if best.get("same_work") else "open",
            }

        return fake

    def _patches(self, vision, ocr, rank):
        return mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(return_value=None),
            identify_code=mock.Mock(side_effect=self._identify),
            fetch_avbase_title_results=mock.Mock(side_effect=self._fetch),
            rank_candidates_by_visual=mock.Mock(side_effect=rank),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        )

    def test_prompt_prefers_code_over_decorative_and_chrome(self):
        prompt = S.VISION_PROMPT
        self.assertIn("APGH-012", prompt)
        self.assertIn("舌技", prompt)
        self.assertIn("無碼影片", prompt)
        self.assertIn("ApGH-012", prompt)
        self.assertIn("texts", prompt)
        # Full dump is cover-only. Listing and UI must not pour every line in.
        self.assertIn("shot=cover", prompt)
        self.assertIn("listing", prompt)
        self.assertIn("不要把整頁字", prompt)

    def test_code_shapes_and_chrome_are_not_titles(self):
        for raw in ("APGH-012", "ApGH-012", "APGH 012", "APGH012"):
            codes = S.extract_codes(raw)
            self.assertEqual(codes, ["APGH-012"], raw)
        parsed = S.parse_vision_json(
            '{"code":"ApGH-012","title":"無碼影片","actress":"柊ゆうき","confidence":0.8}'
        )
        self.assertEqual(parsed.get("code"), "APGH-012")
        self.assertTrue(S._is_decorative_overlay("舌技が神"))
        self.assertTrue(S._is_site_chrome_title("無碼影片"))
        self.assertFalse(S._is_decorative_overlay(APGH_TITLE))
        self.assertEqual(
            S._title_from_ocr_text("無碼影片\n2:25:56\n舌技が神\nYuuki Hiiragi"),
            "舌技が神",
        )
        sole, many = S._sole_product_code("APGH-025\nAPGH-012\nAPGH-015")
        self.assertIsNone(sole)
        self.assertEqual(many, ["APGH-025", "APGH-012", "APGH-015"])
        parsed = S.parse_vision_json(
            '{"title":"舌技が神","texts":["舌技が神","先生が2人っきりの","APGH-012","オーロラ"],"actress":"柊ゆうき"}'
        )
        self.assertEqual(parsed.get("code"), "APGH-012")
        self.assertIn("先生が2人っきりの", parsed.get("texts") or [])
        self.assertIn("舌技が神", parsed.get("texts") or [])
        listed = S.parse_vision_json('{"shot":"LISTING","title":"舌技が神"}')
        self.assertEqual(listed.get("shot"), "listing")
        unknown = S.parse_vision_json('{"shot":"poster","title":"舌技が神"}')
        self.assertEqual(unknown.get("shot"), "")

    def test_listing_caption_code_beats_slogan(self):
        img = b"missav-apgh-012-card"

        def vision(image_bytes, mime, api_key):
            return {"title": "舌技が神", "actress": ACTRESS}

        def ocr(image_bytes):
            return "APGH-012\nYuuki Hiiragi\n舌技が神\n2:25:56\n無碼影片"

        with self._patches(vision, ocr, self._rank(APGH_CODE)):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="card.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertNotIn("舌技が神", str(payload.get("title") or ""))
        self.assertTrue(payload.get("cover"))
        self.assertFalse(payload.get("needs_code"))
        self.assertNotEqual(payload.get("code"), "TITLE-SEARCH")

    def test_weird_code_and_badge_title_still_resolve(self):
        img = b"apgh-badge"

        def vision(image_bytes, mime, api_key):
            return {"code": "ApGH-012", "title": "無碼影片", "actress": "Yuuki Hiiragi"}

        with self._patches(vision, lambda image_bytes: "", self._rank(APGH_CODE)):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="card.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertNotEqual(payload.get("title"), "無碼影片")

    def test_actress_visual_lock_when_slogan_is_not_the_catalog_title(self):
        img = b"cover-only-tongue"

        def vision(image_bytes, mime, api_key):
            return {"title": "舌技が神", "actress": ACTRESS}

        def ocr(image_bytes):
            return "舌技が神"

        with self._patches(vision, ocr, self._rank(APGH_CODE)):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="cover.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertTrue(payload.get("visual_lock"))
        self.assertEqual(payload.get("search_mode"), "actress")
        self.assertNotIn("舌技", payload.get("title") or "")

    def test_listing_grid_locks_the_thumb_instead_of_the_first_code(self):
        img = b"missav-grid"

        def vision(image_bytes, mime, api_key):
            return {"title": "舌技が神", "actress": ACTRESS}

        def ocr(image_bytes):
            return "APGH-025 老師會在只\nAPGH-022\nAPGH-015\nAPGH-012 Yuuki Hiiragi\n舌技"

        with self._patches(vision, ocr, self._rank(APGH_CODE)):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="grid.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertNotEqual(payload.get("code"), "APGH-025")
        self.assertEqual(payload.get("search_mode"), "code")

    def test_cover_only_corner_code_is_not_a_listing_row(self):
        """品番 is small print on the art itself, not a missav caption under the thumb."""
        img = b"cover-art-with-corner-code"

        def vision(image_bytes, mime, api_key):
            return {"title": "舌技が神", "actress": ACTRESS, "texts": ["舌技が神"]}

        def ocr(image_bytes):
            return "舌技が神\nオーロラ\nAPGH-012"

        searches: list[str] = []

        def search(title, actress=None):
            searches.append(str(title or ""))
            return None

        with mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=search),
            identify_code=mock.Mock(side_effect=self._identify),
            fetch_avbase_title_results=mock.Mock(return_value=[]),
            rank_candidates_by_visual=mock.Mock(side_effect=self._rank(None)),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        ):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="cover.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertEqual(searches, [])

    def test_full_text_gate_requires_cover_and_no_listing_row(self):
        """Do not drop this gate: both a complete cover and no caption row."""
        fragments = "舌技が神\n先生が2人っきりの\nプライベート補習"
        self.assertTrue(S._cover_full_text_allowed({"shot": "cover"}, fragments))
        self.assertFalse(S._cover_full_text_allowed({}, fragments))
        self.assertFalse(S._cover_full_text_allowed({"shot": ""}, fragments))
        self.assertFalse(S._cover_full_text_allowed({"shot": "listing"}, fragments))
        self.assertFalse(S._cover_full_text_allowed({"shot": "ui"}, fragments))
        caption = "APGH-012 Yuuki Hiiragi\n舌技が神"
        self.assertTrue(S._listing_chrome_in_text(caption))
        self.assertFalse(S._cover_full_text_allowed({"shot": "cover"}, caption))
        self.assertFalse(
            S._cover_full_text_allowed({"shot": "cover"}, "舌技が神\n2:25:56\n無碼影片")
        )
        self.assertFalse(
            S._cover_full_text_allowed({"shot": "cover"}, "LIVE\n先生が2人っきりの")
        )
        self.assertTrue(
            S._listing_chrome_in_text("APGH-012\nAPGH-015\n先生が2人っきりの")
        )

    def test_cover_only_full_text_beats_the_slogan(self):
        """No 品番 on the art. The catalog line is smaller print than 舌技が神.

        shot=cover is required. Without it this same OCR must not be searched
        as a blob (see the listing/grid cases).
        """
        img = b"cover-art-title-fragments"
        seen: list[str] = []

        def vision(image_bytes, mime, api_key):
            return {
                "shot": "cover",
                "title": "舌技が神",
                "actress": ACTRESS,
                "texts": ["舌技が神"],
            }

        def ocr(image_bytes):
            return "舌技が神\n先生が2人っきりの\nプライベート補習\nオーロラ"

        def search(title, actress=None):
            q = re.sub(r"\s+", "", str(title or ""))
            seen.append(q)
            official = re.sub(r"\s+", "", APGH_TITLE)
            if "舌技" in q and "先生" not in q and "補習" not in q:
                return None
            if len(q) >= 6 and q in official:
                return _work(APGH_CODE, APGH_TITLE)
            return None

        with mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=search),
            identify_code=mock.Mock(side_effect=self._identify),
            fetch_avbase_title_results=mock.Mock(return_value=[]),
            rank_candidates_by_visual=mock.Mock(side_effect=self._rank(None)),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        ):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="cover.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertTrue(any("先生" in q or "補習" in q for q in seen))
        self.assertNotEqual(seen[:1], ["舌技が神"])
        self.assertNotIn("Yuuki", "\n".join(seen))

    def _run_recorded_search(self, vision, ocr, seen: list[str]):
        def search(title, actress=None):
            seen.append(re.sub(r"\s+", "", str(title or "")))
            return None

        with mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=search),
            identify_code=mock.Mock(side_effect=self._identify),
            fetch_avbase_title_results=mock.Mock(return_value=[]),
            rank_candidates_by_visual=mock.Mock(side_effect=self._rank(None)),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        ):
            return S.run_identify_pipeline(image_bytes=b"shot", filename="shot.jpg")

    def test_listing_grid_and_ui_do_not_dump_every_line(self):
        """Do not drop this gate. Neighbor rows stay out of search."""
        neighbor = "別作品の家庭教師は今日も居残り"
        ocr_text = "舌技が神\n先生が2人っきりの\n" + neighbor + "\n2:25:56\n無碼影片"

        def ocr(image_bytes):
            return ocr_text

        for shot in ("listing", "ui", ""):
            seen: list[str] = []

            def vision(image_bytes, mime, api_key, shot=shot):
                row = {
                    "title": "舌技が神",
                    "actress": ACTRESS,
                    "texts": ["舌技が神", "先生が2人っきりの", neighbor],
                }
                if shot:
                    row["shot"] = shot
                return row

            payload, status = self._run_recorded_search(vision, ocr, seen)
            blob = "\n".join(seen)
            self.assertEqual(status, 200, shot)
            self.assertNotIn("先生", blob, shot)
            self.assertNotIn("補習", blob, shot)
            self.assertNotIn("家庭教師", blob, shot)
            self.assertEqual(payload.get("code"), "TITLE-SEARCH", shot)

    def test_cover_label_with_caption_row_does_not_dump_text(self):
        """shot=cover is not enough when a missav row sits under the thumb.

        The caption 品番 is still used. Neighbor title lines are not searched.
        Do not drop this gate.
        """
        seen: list[str] = []

        def vision(image_bytes, mime, api_key):
            return {
                "shot": "cover",
                "title": "舌技が神",
                "actress": ACTRESS,
                "texts": ["舌技が神", "先生が2人っきりの", "別作品の家庭教師は今日も居残り"],
            }

        def ocr(image_bytes):
            return (
                "APGH-012 Yuuki Hiiragi\n舌技が神\n先生が2人っきりの\n"
                "別作品の家庭教師は今日も居残り"
            )

        payload, status = self._run_recorded_search(vision, ocr, seen)
        blob = "\n".join(seen)
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), APGH_CODE)
        self.assertEqual(payload.get("title"), APGH_TITLE)
        self.assertNotIn("先生", blob)
        self.assertNotIn("家庭教師", blob)

    def test_multi_listing_does_not_search_neighbor_rows(self):
        """Do not drop this gate on the multi path either."""
        img_list = b"listing-rows"
        img_cover = b"full-jacket"
        neighbor = "別作品の家庭教師は今日も居残り"
        seen: list[str] = []

        def vision(image_bytes, mime, api_key):
            if image_bytes == img_cover:
                return {
                    "shot": "cover",
                    "title": "舌技が神",
                    "actress": ACTRESS,
                    "texts": ["舌技が神"],
                }
            return {
                "shot": "listing",
                "title": "舌技が神",
                "actress": ACTRESS,
                "texts": ["舌技が神", "先生が2人っきりの", neighbor],
            }

        def ocr(image_bytes):
            if image_bytes == img_cover:
                return "舌技が神\n先生が2人っきりの\nプライベート補習\nオーロラ"
            return "舌技が神\n先生が2人っきりの\n" + neighbor + "\n2:25:56"

        def search(title, actress=None):
            q = re.sub(r"\s+", "", str(title or ""))
            seen.append(q)
            official = re.sub(r"\s+", "", APGH_TITLE)
            if len(q) >= 6 and q in official:
                return _work(APGH_CODE, APGH_TITLE)
            return None

        with mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=search),
            identify_code=mock.Mock(side_effect=self._identify),
            fetch_avbase_title_results=mock.Mock(return_value=[]),
            rank_candidates_by_visual=mock.Mock(side_effect=self._rank(None)),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        ):
            payload, status = S.run_multi_identify_pipeline(
                [(img_list, "list.jpg"), (img_cover, "cover.jpg")]
            )

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2, payload.get("related_note"))
        self.assertEqual(results[0].get("code"), "TITLE-SEARCH")
        self.assertEqual(results[1].get("code"), APGH_CODE)
        blob = "\n".join(seen)
        self.assertNotIn("家庭教師", blob)
        self.assertTrue(any("先生" in q or "補習" in q for q in seen))

    def test_no_visual_lock_stays_title_only(self):
        img = b"cover-only-no-lock"

        def vision(image_bytes, mime, api_key):
            return {"title": "舌技が神", "actress": ACTRESS}

        with self._patches(vision, lambda image_bytes: "舌技が神", self._rank(None)):
            payload, status = S.run_identify_pipeline(image_bytes=img, filename="cover.jpg")

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), "TITLE-SEARCH")
        self.assertTrue(payload.get("needs_code"))
        self.assertNotEqual(payload.get("code"), APGH_CODE)
        self.assertIn("舌技", payload.get("title") or "")

    def test_multi_listing_keeps_apgh_beside_other_frames(self):
        img_swim = b"school-swimsuit-cover"
        img_list = b"missav-apgh-012-card"
        img_crop = b"yellow-bikini-crop"

        def vision(image_bytes, mime, api_key):
            if image_bytes == img_list:
                return {"title": "舌技が神", "actress": ACTRESS}
            if image_bytes == img_crop:
                return {"code": "JULX-271"}
            return {}

        def ocr(image_bytes):
            if image_bytes == img_swim:
                return "巨乳水泳部員の媚薬合宿記録"
            if image_bytes == img_list:
                return "APGH-012 Yuuki Hiiragi\n舌技が神\n無碼影片"
            return ""

        def identify(code, ocr_preview=None, vision_meta=None):
            disp = S.format_display_code(str(code))
            if disp == "SWIM-804":
                return _work("SWIM-804", "巨乳水泳部員の媚薬合宿記録")
            if disp == "JULX-271":
                return _work("JULX-271", "地味な眼鏡では隠し切れない美人OLの完全生撮り")
            return self._identify(code)

        def search(title, actress=None):
            text = str(title or "")
            if "媚薬" in text or "水泳部" in text:
                return _work("SWIM-804", "巨乳水泳部員の媚薬合宿記録")
            return None

        def rank(user, cands, api_key=None, **kwargs):
            want = {
                img_swim: "SWIM-804",
                img_list: APGH_CODE,
                img_crop: "JULX-271",
            }.get(user)
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                code = S.format_display_code(str(item.get("code") or ""))
                take = bool(want) and code == want
                item["visual"] = _lock(take)
                item["visual_score"] = 0.9 if take else 0.1
                ranked.append(item)
            ranked.sort(key=lambda it: float(it.get("visual_score") or 0), reverse=True)
            best = (ranked[0].get("visual") if ranked else {}) or {}
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": bool(best.get("same_work") and best.get("match_clothes")),
                "compared": len(ranked),
                "note": "",
            }

        with mock.patch.multiple(
            S,
            get_gemini_api_key=mock.Mock(return_value="test-key"),
            call_gemini_vision=mock.Mock(side_effect=vision),
            ocr_image_bytes=mock.Mock(side_effect=ocr),
            search_by_title=mock.Mock(side_effect=search),
            identify_code=mock.Mock(side_effect=identify),
            fetch_avbase_title_results=mock.Mock(side_effect=self._fetch),
            rank_candidates_by_visual=mock.Mock(side_effect=rank),
            offline_cache_get=mock.Mock(return_value=None),
            offline_cache_put=mock.Mock(return_value=None),
            probe_cover_url=mock.Mock(side_effect=lambda url, timeout=0: (True, url)),
            resolve_chinese_title=mock.Mock(return_value=None),
            find_related_by_title=mock.Mock(return_value=[]),
            attach_related_by_title=mock.Mock(side_effect=lambda result, **kwargs: result),
        ):
            payload, status = S.run_multi_identify_pipeline(
                [(img_swim, "swim.jpg"), (img_list, "list.jpg"), (img_crop, "crop.jpg")]
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("dropped"), [])
        results = payload.get("results") or []
        self.assertEqual(len(results), 3, payload.get("related_note"))
        codes = [row.get("code") for row in results]
        self.assertEqual(codes, ["SWIM-804", APGH_CODE, "JULX-271"])
        self.assertEqual(results[1].get("title"), APGH_TITLE)
        self.assertFalse(results[1].get("needs_code"))
        note = (payload.get("related_note") or "") + (payload.get("message") or "")
        self.assertNotIn("略過", note)
        self.assertNotIn("去重", note)
        self.assertNotIn("合併", note)


if __name__ == "__main__":
    unittest.main()
