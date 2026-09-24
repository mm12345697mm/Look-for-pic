"""OCR junk must not become a title or a chip, and a 2-image batch must finish.

A cover read of 特別な補習 "< ey used to be searched as a title, which chipped
the latin crumb EY and left the catalog empty. A shared identify clock must
not mark a later frame 尚未查完 while a normal catalog lookup would still fit.
"""

from __future__ import annotations

import io
import time
import unittest
from unittest import mock

from PIL import Image

import server as S


def _png(color: tuple[int, int, int] = (20, 40, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (80, 80), color).save(buf, format="PNG")
    return buf.getvalue()


LONG = "先生が二人っきりの架空補習で全部面倒みてあげる長い題"
GARBAGE = '特別な補習 "< ey'


class TestGarbageTitleRejection(unittest.TestCase):
    def test_ascii_junk_is_not_a_title_or_a_chip(self):
        self.assertTrue(S._title_has_ocr_garbage(GARBAGE))
        self.assertFalse(S.is_usable_title(GARBAGE))
        self.assertIsNone(S._strip_ocr_garbage_title(GARBAGE))
        chips = S._extract_title_theme_keywords(GARBAGE)
        self.assertNotIn("EY", chips)
        self.assertNotIn("ey", [c.casefold() for c in chips])
        self.assertFalse(any("<" in c or '"' in c for c in chips))
        self.assertFalse(S._keyword_token_ok("EY"))

    def test_known_latin_themes_stay_chips(self):
        self.assertIn("OL", S._extract_title_theme_keywords("美人OL"))
        self.assertIn("VR", S._extract_title_theme_keywords("VRで巨乳"))
        self.assertIn("NTR", S._extract_title_theme_keywords("逆NTRの巨乳"))
        self.assertFalse(S._title_has_ocr_garbage("美人OL"))
        self.assertTrue(S.is_usable_title("美人OLの物語"))

    def test_readable_line_replaces_the_junk_suffix(self):
        blob = "\n".join([GARBAGE, LONG, "架空ゆうき"])
        replaced = S._replace_garbage_title(GARBAGE, blob, actress="架空ゆうき")
        self.assertEqual(replaced, LONG)
        repaired = S._strip_ocr_garbage_title(LONG + ' "< ey')
        self.assertEqual(repaired, LONG)

    def test_multi_cover_searches_the_clean_line_not_the_crumb(self):
        cover = _png((30, 10, 10))
        swim = _png((10, 30, 80))
        seen: list[str] = []

        def vision(image_bytes, mime, api_key, timeout=None, max_models=None):
            if image_bytes == cover:
                return {
                    "title": GARBAGE,
                    "actress": "架空ゆうき",
                    "shot": "cover",
                    "texts": [GARBAGE, LONG, "架空ゆうき", "舌技が神"],
                }
            return {"code": "SER-221", "title": "巨乳水泳部員の架空合宿記録です", "shot": "cover"}

        def identify(**kwargs):
            title = str(kwargs.get("user_title") or "")
            code = str(kwargs.get("user_code") or "")
            seen.append(title or code)
            if code:
                return {
                    "ok": True,
                    "code": code,
                    "title": "巨乳水泳部員の架空合宿記録です",
                    "actress": "架空泳子",
                }, 200
            self.assertNotIn('"<', title)
            self.assertNotIn("ey", title.casefold())
            self.assertIn("長い題", title)
            return {"ok": True, "code": "SER-880", "title": LONG, "actress": "架空ゆうき"}, 200

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=vision
        ), mock.patch.object(S, "ocr_image_bytes", return_value=""), mock.patch.object(
            S, "run_identify_pipeline", side_effect=identify
        ), mock.patch.object(
            S, "apply_visual_rank_to_hit", side_effect=lambda hit, *a, **k: hit
        ), mock.patch.object(
            S, "verify_work_against_image", side_effect=lambda result, *a, **k: result
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline(
                [(cover, "cover.png"), (swim, "swim.png")],
                deadline=time.monotonic() + 90,
            )

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2, payload.get("message"))
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        titles = " ".join(seen)
        self.assertNotIn('"<', titles)
        self.assertNotIn("ey", titles.casefold())
        self.assertTrue(any("長い題" in item or item == "SER-880" for item in seen), seen)
        self.assertTrue(any(item == "SER-221" for item in seen), seen)
        for row in results:
            shown = str(row.get("title") or "")
            self.assertNotIn("EY", S._extract_title_theme_keywords(shown))
            self.assertNotIn('"<', shown)


class TestSmallBatchDoesNotFalseTimeout(unittest.TestCase):
    def _images(self, n: int) -> list[tuple[bytes, str]]:
        return [(_png((20 + i, 40, 80)), f"slot-{i}.png") for i in range(n)]

    def test_two_slots_finish_when_mocks_are_fast_inside_the_related_reserve(self):
        """18s is enough for two instant lookups. It is not enough to also
        reserve a 20s related window, which used to skip the second frame.
        """
        calls: list[str] = []

        def identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            calls.append(code)
            return {
                "ok": True,
                "code": code,
                "title": "架空題名のテスト作品です",
            }, 200

        images = self._images(2)
        with mock.patch.object(S, "get_gemini_api_key", return_value=""), mock.patch.object(
            S, "ocr_image_bytes", side_effect=["SER-221", "SER-880"]
        ), mock.patch.object(S, "run_identify_pipeline", side_effect=identify), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
        ), mock.patch.object(S, "offline_cache_get", return_value=None):
            payload, status = S.run_multi_identify_pipeline(
                images,
                deadline=time.monotonic() + 18,
            )
        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        self.assertEqual(calls, ["SER-221", "SER-880"])
        self.assertFalse(payload.get("partial"))

    def test_expired_deadline_does_not_skip_the_second_slot(self):
        calls: list[str] = []

        def identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            calls.append(code)
            return {"ok": True, "code": code, "title": "架空題名のテスト作品です"}, 200

        images = self._images(2)
        with mock.patch.object(S, "get_gemini_api_key", return_value=""), mock.patch.object(
            S, "ocr_image_bytes", side_effect=["SER-221", "SER-880"]
        ), mock.patch.object(S, "run_identify_pipeline", side_effect=identify), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
        ):
            payload, status = S.run_multi_identify_pipeline(
                images,
                deadline=time.monotonic() - 1,
            )
        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2)
        self.assertEqual(calls, ["SER-221", "SER-880"])
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        self.assertFalse(payload.get("partial"))

    def test_near_deadline_still_starts_visual_lock(self):
        front = _png()
        hit = {
            "code": "SER-001",
            "title": "架空題名のテスト作品です",
            "candidates": [
                {"code": "SER-001", "title": "架空題名のテスト作品です"},
                {"code": "SER-002", "title": "架空題名のテスト作品です"},
            ],
        }
        ranked = [
            {
                "code": "SER-002",
                "title": "架空題名のテスト作品です",
                "visual": {
                    "same_work": True,
                    "match_clothes": True,
                    "match_person": True,
                    "confidence": 0.9,
                },
            }
        ]

        seen: dict = {}

        def fake_rank(*args, **kwargs):
            seen["budget"] = kwargs.get("budget_s")
            seen["n"] = len(args[1]) if len(args) > 1 else None
            return ranked, {"visual_ranked": True, "visual_lock": True, "compared": 2}

        jacket_calls: list = []

        def fake_jacket(*args, **kwargs):
            jacket_calls.append(args)
            return None

        # Five seconds is far under the old 40s gate. The compare still runs
        # at the full #25 budget, after the jacket lock, and is not shortened.
        S._enter_batch_ctx(time.monotonic() + 5)
        try:
            self.assertFalse(S._visual_rank_blocked(2))
            self.assertFalse(S._visual_rank_blocked(8))
            with mock.patch.object(S, "_jacket_lock_winner", side_effect=fake_jacket), mock.patch.object(
                S, "rank_candidates_by_visual", side_effect=fake_rank
            ):
                out = S.apply_visual_rank_to_hit(dict(hit), front, api_key="k")
            self.assertEqual(len(jacket_calls), 1)
            self.assertEqual(seen.get("budget"), S.VISUAL_COMPARE_BUDGET)
            self.assertEqual(seen.get("n"), 2)
            self.assertFalse(out.get("lock_incomplete"))
            self.assertEqual(out.get("code"), "SER-002")
        finally:
            S._leave_batch_ctx()

    def test_cut_jacket_still_refuses_to_guess_on_a_small_batch(self):
        front = _png()
        hit = {
            "code": "SER-001",
            "title": "架空題名のテスト作品です",
            "candidates": [
                {"code": "SER-001", "title": "架空題名のテスト作品です"},
                {"code": "SER-004", "title": "架空題名のテスト作品です"},
            ],
        }

        def fake_lock(*args, **kwargs):
            S._BATCH.jacket_incomplete = True
            return None

        S._enter_batch_ctx(time.monotonic() + 80)
        try:
            with mock.patch.object(S, "_jacket_lock_winner", side_effect=fake_lock), mock.patch.object(
                S, "rank_candidates_by_visual", side_effect=AssertionError("cut jacket")
            ):
                out = S.apply_visual_rank_to_hit(hit, front, api_key="k")
            self.assertTrue(out.get("lock_incomplete"))
            self.assertFalse(out.get("visual_lock"))
        finally:
            S._leave_batch_ctx()


if __name__ == "__main__":
    unittest.main()
