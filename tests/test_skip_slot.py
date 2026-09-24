#!/usr/bin/env python3
"""Skip the current multi-identify slot and keep a placeholder for later retry.

The rest of the batch keeps going. N stays the upload count. A skipped card
is not a timeout, and its upload preview is not the catalog cover.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402

FALSE_TIMEOUT = ("時間不夠", "尚未查完", "尚未鎖定")


def _jpeg(tag: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (12, 8), (20 + tag * 40, 30, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestSkipSlot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._jobs = S._IDENTIFY_JOBS_PATH
        self._sessions = S._IDENTIFY_SESSIONS_PATH
        S._IDENTIFY_JOBS_PATH = Path(self.tmp.name) / "jobs.json"
        S._IDENTIFY_SESSIONS_PATH = Path(self.tmp.name) / "sessions.json"

    def tearDown(self):
        S._IDENTIFY_JOBS_PATH = self._jobs
        S._IDENTIFY_SESSIONS_PATH = self._sessions
        self.tmp.cleanup()

    def test_skip_api_marks_only_the_active_slot(self):
        job_id = S.identify_job_create(7)
        self.assertTrue(job_id)
        idle = S.identify_job_skip_current(job_id)
        self.assertFalse(idle.get("ok"))
        S.identify_job_set_active(job_id, 3, "search")
        out = S.identify_job_skip_current(job_id)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("skipped_index"), 3)
        self.assertEqual(out.get("image_count"), 7)
        self.assertTrue(S.identify_job_is_skipped(job_id, 3))
        self.assertFalse(S.identify_job_is_skipped(job_id, 4))
        again = S.identify_job_skip_current(job_id)
        self.assertEqual(again.get("skipped_index"), 3)
        self.assertEqual(S.identify_job_public(job_id).get("skip_indexes"), [3])
        S.identify_job_set_active(job_id, None, "related")
        refused = S.identify_job_skip_current(job_id)
        self.assertFalse(refused.get("ok"))
        self.assertEqual(S.identify_job_public(job_id).get("image_count"), 7)

    def test_skip_mid_search_keeps_placeholder_and_original_denominator(self):
        slots = [
            ("AAA-001", "巨乳水泳部員の媚薬合宿記録"),
            ("BBB-002", "家庭教師の肉欲教育"),
            ("CCC-003", "満員電車の痴漢"),
        ]
        titles = {code: title for code, title in slots}
        images = [(_jpeg(i), f"slot{i}.jpg") for i in range(3)]
        by_bytes = {blob: slots[i] for i, (blob, _name) in enumerate(images)}
        identified: list[str] = []
        details: list[str] = []
        release = threading.Event()
        job_id = S.identify_job_create(3)

        def on_progress(evt):
            if isinstance(evt, dict) and evt.get("detail"):
                details.append(str(evt["detail"]))

        def fake_vision(image_bytes, mime, api_key):
            code, title = by_bytes[image_bytes]
            return {"code": code, "title": title, "actress": "誰か"}

        def fake_identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            identified.append(code)
            if code == "BBB-002":
                S.identify_job_skip_current(job_id)
                release.wait(5)
            cid = code.lower().replace("-", "")
            return {
                "ok": True,
                "code": code,
                "title": titles[code],
                "actress": "誰か",
                "cid": cid,
                "cover": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg",
                "stills": [],
                "related_by_title": [
                    {
                        "code": "REL-" + code[:3],
                        "title": "相關" + code,
                        "line": "keyword",
                        "cover": "https://pics.dmm.co.jp/digital/video/rel00001/rel00001pl.jpg",
                    }
                ],
            }, 200

        def fake_related(result, **kwargs):
            out = dict(result)
            out["related_by_title"] = list(result.get("related_by_title") or [])
            return out

        started = time.monotonic()
        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_identify
        ), mock.patch.object(S, "verify_work_against_image", side_effect=lambda hit, *a, **k: hit), mock.patch.object(
            S, "attach_related_by_title", side_effect=fake_related
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put", return_value=None
        ), mock.patch.object(S, "_escalate_frame_title", return_value=None), mock.patch.object(
            S, "_recover_locked_work", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline(
                images, on_progress=on_progress, job_id=job_id
            )
            release.set()
            time.sleep(0.2)
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertLess(elapsed, 1.8, "skip must leave the slow slot instead of waiting it out")
        self.assertIn("AAA-001", identified)
        self.assertIn("CCC-003", identified)
        self.assertLess(identified.index("AAA-001"), identified.index("CCC-003"))
        results = payload.get("results") or []
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].get("code"), "AAA-001")
        self.assertEqual(results[2].get("code"), "CCC-003")
        self.assertTrue(results[0].get("related_by_title"))
        self.assertTrue(results[2].get("related_by_title"))
        self.assertEqual(results[0]["related_by_title"][0]["code"], "REL-AAA")
        skipped = results[1]
        self.assertFalse(skipped.get("ok"))
        self.assertTrue(skipped.get("skipped"))
        self.assertEqual(skipped.get("from_image_index"), 2)
        self.assertFalse(skipped.get("code"))
        self.assertFalse(skipped.get("title"))
        self.assertFalse(skipped.get("cover"))
        self.assertTrue(str(skipped.get("user_preview") or "").startswith("data:image/jpeg;base64,"))
        self.assertNotEqual(skipped.get("cover"), skipped.get("user_preview"))
        self.assertEqual(skipped.get("why"), "已跳過")
        self.assertIn("已跳過", str(payload.get("message") or ""))
        blob = "\n".join(details) + "\n" + str(payload.get("message") or "")
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, blob)
        for detail in details:
            if "第" in detail and "/" in detail:
                denom = detail.split("/")[1]
                digits = ""
                for ch in denom:
                    if ch.isdigit():
                        digits += ch
                    elif digits:
                        break
                self.assertEqual(digits, "3", detail)
        self.assertTrue(any("已跳過第 2/3 張" in d for d in details), details)
        self.assertTrue(any(d.startswith("搜尋第 1/3") for d in details), details)
        self.assertTrue(any(d.startswith("搜尋第 3/3") for d in details), details)
        self.assertFalse(any(d.startswith("搜尋第 2/2") or "/2 張" in d for d in details), details)
        self.assertEqual(payload.get("image_count"), 3)
        self.assertEqual(S.SLOT_WORK_BUDGET_S, 600.0)
        self.assertEqual(S.GUNICORN_WORKER_TIMEOUT_S, 2400)

    def test_skip_during_vision_does_not_catalog_that_upload(self):
        slots = [("AAA-001", "作品甲"), ("BBB-002", "作品乙"), ("CCC-003", "作品丙")]
        images = [(_jpeg(i), f"v{i}.jpg") for i in range(3)]
        by_bytes = {blob: slots[i] for i, (blob, _name) in enumerate(images)}
        identified: list[str] = []
        release = threading.Event()
        job_id = S.identify_job_create(3)

        def fake_vision(image_bytes, mime, api_key):
            code, title = by_bytes[image_bytes]
            if code == "BBB-002":
                S.identify_job_skip_current(job_id)
                release.wait(5)
            return {"code": code, "title": title, "actress": "誰か"}

        def fake_identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            identified.append(code)
            cid = code.lower().replace("-", "")
            return {
                "ok": True,
                "code": code,
                "title": code,
                "cid": cid,
                "cover": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg",
            }, 200

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_identify
        ), mock.patch.object(S, "verify_work_against_image", side_effect=lambda hit, *a, **k: hit), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda result, **k: result
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put", return_value=None
        ), mock.patch.object(S, "_escalate_frame_title", return_value=None), mock.patch.object(
            S, "_recover_locked_work", return_value=None
        ):
            payload, status = S.run_multi_identify_pipeline(images, job_id=job_id)
            release.set()
            time.sleep(0.2)

        self.assertEqual(status, 200)
        self.assertEqual(identified, ["AAA-001", "CCC-003"])
        skipped = (payload.get("results") or [])[1]
        self.assertTrue(skipped.get("skipped"))
        self.assertFalse(skipped.get("cover"))
        self.assertEqual(skipped.get("from_image_index"), 2)

    def test_reidentify_patches_one_session_frame(self):
        record = S._build_identify_session(
            {"image_count": 3, "result_count": 3, "message": "多圖", "ok": True},
            [
                {"index": 1, "final_code": "AAA-001", "final_title": "甲", "skipped": False},
                {"index": 2, "final_code": None, "final_title": None, "skipped": True},
                {"index": 3, "final_code": "CCC-003", "final_title": "丙", "skipped": False},
            ],
        )
        saved = S.identify_session_put(record)
        self.assertTrue(saved)
        single = {
            "ok": True,
            "code": "BBB-002",
            "title": "乙",
            "cover": "https://pics.dmm.co.jp/digital/video/bbb00002/bbb00002pl.jpg",
        }
        self.assertTrue(S.identify_session_patch_slot(saved["id"], 2, single))
        fresh = S.identify_session_get(saved["id"])
        frames = fresh.get("frames") or []
        self.assertEqual(frames[0].get("final_code"), "AAA-001")
        self.assertEqual(frames[1].get("final_code"), "BBB-002")
        self.assertEqual(frames[1].get("final_title"), "乙")
        self.assertFalse(frames[1].get("skipped"))
        self.assertEqual(frames[2].get("final_code"), "CCC-003")
        self.assertEqual(frames[2].get("final_title"), "丙")


if __name__ == "__main__":
    unittest.main()
