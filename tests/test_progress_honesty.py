#!/usr/bin/env python3
"""Identify progress rows close in order, and the cover clock wraps the jacket fetch."""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


DMM_COVER = "https://pics.dmm.co.jp/digital/video/royd00343/royd00343pl.jpg"


def _pairs(events: list[dict]) -> list[tuple[str, str]]:
    return [(str(e.get("step")), str(e.get("status"))) for e in events]


def _first(events: list[dict], step: str, status: str | None = None) -> dict:
    for evt in events:
        if evt.get("step") != step:
            continue
        if status is None or evt.get("status") == status:
            return evt
    raise AssertionError(f"missing {step} {status or ''}: {events}")


class ProgressHonestyTests(unittest.TestCase):
    def tearDown(self):
        while getattr(S._job_zh_state(), "depth", 0):
            S._job_zh_end()

    def test_manual_code_closes_parse_before_verify_and_times_cover(self):
        events: list[dict] = []

        def slow_sanitize(code=None, cid=None, cover=None):
            time.sleep(0.05)
            return ("royd00343", DMM_COVER, [DMM_COVER])

        with mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put", return_value=None
        ), mock.patch.object(
            S,
            "fetch_avbase_by_code",
            return_value={"title": "電影院", "actress": "誰か", "source": "avbase"},
        ), mock.patch.object(
            S, "sanitize_cover_fields", side_effect=slow_sanitize
        ), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda item, **_k: item
        ), mock.patch.object(S, "_fill_unfetched_title_zh", return_value=None), mock.patch.object(
            S, "find_related_by_title", return_value=[]
        ), mock.patch.object(S, "http_get", return_value=None):
            payload, status = S.run_identify_pipeline(
                user_code="ROYD-343", on_progress=events.append
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), "ROYD-343")
        self.assertEqual(payload.get("cover"), DMM_COVER)
        self.assertTrue(str(payload.get("cover")).startswith("https://"))
        self.assertFalse(str(payload.get("cover") or "").startswith(("data:", "blob:")))

        pairs = _pairs(events)
        parse_done_at = pairs.index(("parse", "done"))
        verify_at = next(i for i, pair in enumerate(pairs) if pair[0] == "verify")
        search_at = next(i for i, pair in enumerate(pairs) if pair[0] == "search")
        self.assertLess(parse_done_at, verify_at)
        self.assertLess(parse_done_at, search_at)
        self.assertNotIn(("parse", "active"), pairs[parse_done_at:])
        self.assertEqual(_first(events, "parse", "done").get("detail"), "番號：ROYD-343")
        self.assertLess(float(_first(events, "parse", "done")["progress"]), float(_first(events, "verify")["progress"]))

        search_done = _first(events, "search", "done")
        cover_active = _first(events, "cover", "active")
        cover_done = _first(events, "cover", "done")
        self.assertLessEqual(search_done["t_ms"], cover_active["t_ms"])
        self.assertGreaterEqual(cover_done["t_ms"] - cover_active["t_ms"], 40)
        self.assertIn("封面就緒", str(cover_done.get("detail")))
        self.assertEqual(pairs.count(("cover", "active")), 1)
        self.assertEqual(pairs.count(("cover", "done")), 1)

    def test_image_read_closes_parse_before_verify(self):
        events: list[dict] = []

        def slow_sanitize(code=None, cid=None, cover=None):
            time.sleep(0.05)
            return ("royd00343", DMM_COVER, [DMM_COVER])

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S,
            "call_gemini_vision",
            return_value={"code": "ROYD-343", "title": "映画館", "shot": "cover", "texts": ["ROYD-343"]},
        ), mock.patch.object(
            S, "_resolve_printed_code", return_value=(None, None, False, False)
        ), mock.patch.object(
            S,
            "verify_code_matches_title",
            return_value={
                "ok": True,
                "code": "ROYD-343",
                "similarity": 1.0,
                "reason": "片名相符",
                "cover_ok": True,
                "title_ok": True,
            },
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put", return_value=None
        ), mock.patch.object(
            S,
            "fetch_avbase_by_code",
            return_value={"title": "映画館", "actress": "誰か", "source": "avbase"},
        ), mock.patch.object(
            S, "sanitize_cover_fields", side_effect=slow_sanitize
        ), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda item, **_k: item
        ), mock.patch.object(S, "_fill_unfetched_title_zh", return_value=None), mock.patch.object(
            S, "find_related_by_title", return_value=[]
        ), mock.patch.object(S, "http_get", return_value=None), mock.patch.object(
            S, "_ensure_image_visual_rank", side_effect=lambda result, *_a, **_k: result
        ):
            payload, status = S.run_identify_pipeline(
                image_bytes=b"\xff\xd8\xff\xd9",
                filename="a.jpg",
                on_progress=events.append,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("code"), "ROYD-343")
        self.assertTrue(str(payload.get("cover") or "").startswith("https://"))
        pairs = _pairs(events)
        parse_active_at = pairs.index(("parse", "active"))
        parse_done_at = pairs.index(("parse", "done"))
        verify_at = next(i for i, pair in enumerate(pairs) if pair[0] == "verify")
        self.assertLess(parse_active_at, parse_done_at)
        self.assertLess(parse_done_at, verify_at)
        self.assertNotIn(("parse", "active"), pairs[parse_done_at:])
        cover_active = _first(events, "cover", "active")
        cover_done = next(e for e in events if e.get("step") == "cover" and e.get("status") in ("done", "skipped"))
        self.assertGreaterEqual(cover_done["t_ms"] - cover_active["t_ms"], 40)

    def test_multi_image_closes_parse_before_verify(self):
        events: list[dict] = []
        images = [(b"\xff\xd8\xff\xd9", "a.jpg"), (b"\xff\xd8\xff\xd9", "b.jpg")]

        def fake_pipeline(**_kwargs):
            return (
                {
                    "ok": True,
                    "code": "MIDA-616",
                    "title": "彼女の妹",
                    "cover": "https://pics.dmm.co.jp/digital/video/mida00616/mida00616pl.jpg",
                    "stills": [],
                    "search_mode": "code",
                },
                200,
            )

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S,
            "call_gemini_vision",
            return_value={"code": "MIDA-616", "title": "彼女の妹", "shot": "cover", "texts": ["MIDA-616"]},
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_pipeline
        ), mock.patch.object(
            S, "verify_work_against_image", side_effect=lambda one, *_a, **_k: one
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda item, **_k: item
        ), mock.patch.object(S, "_probe_related_look_alike", return_value=None), mock.patch.object(
            S, "http_get", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline(images, on_progress=events.append)

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("multi"))
        pairs = _pairs(events)
        parse_done_at = pairs.index(("parse", "done"))
        verify_at = pairs.index(("verify", "active"))
        self.assertLess(parse_done_at, verify_at)
        self.assertNotIn(("parse", "active"), pairs[parse_done_at:])
        self.assertEqual(_first(events, "parse", "done").get("detail"), "待查 2 張")


if __name__ == "__main__":
    unittest.main()
