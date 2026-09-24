#!/usr/bin/env python3
"""Owner-readable per-frame map of an identify session.

The phone's local history is not enough: a later fix has to see which upload
became which card. These records stay behind the owner token.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server as S


def _lock(take: bool) -> dict:
    return {
        "same_work": take,
        "confidence": 0.9 if take else 0.1,
        "match_person": take,
        "match_clothes": take,
        "match_pose": take,
    }


class TestIdentifySessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_path = S._IDENTIFY_SESSIONS_PATH
        S._IDENTIFY_SESSIONS_PATH = Path(self.tmp.name) / "identify-sessions.json"
        self._env = {
            "OWNER_DEVICE_TOKEN": os.environ.get("OWNER_DEVICE_TOKEN"),
            "SITE_PASSWORD": os.environ.get("SITE_PASSWORD"),
        }

    def tearDown(self):
        S._IDENTIFY_SESSIONS_PATH = self._prev_path
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _run_three(self):
        img_swim = b"school-swimsuit-cover"
        img_phrase = b"tongue-title-frame"
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

        def pipe(**kwargs):
            title = str(kwargs.get("user_title") or "")
            code = str(kwargs.get("user_code") or "")
            if "媚薬" in title:
                hit = {"code": "SWIM-804", "title": "巨乳水泳部員の媚薬合宿記録"}
            elif "舌技" in title:
                hit = {"code": "SHJG-448", "title": "舌技が神と呼ばれる架空の夜"}
            elif code == "JULX-271":
                hit = {"code": "JULX-271", "title": "地味な眼鏡では隠し切れない美人OLの完全生撮り"}
            else:
                return S.empty_identify(message="miss"), 200
            hit.update(
                {
                    "ok": True,
                    "actress": "架空",
                    "cover": "https://example.com/" + hit["code"] + ".jpg",
                    "stills": [],
                    "candidates": [dict(hit)],
                }
            )
            return hit, 200

        def rank(user, cands, api_key=None, **kwargs):
            want = expected.get(user)
            ranked = []
            for cand in cands or []:
                item = dict(cand)
                code = S.format_display_code(str(item.get("code") or ""))
                take = bool(want) and code == want
                item["visual"] = _lock(take)
                item["visual_score"] = 0.9 if take else 0.1
                ranked.append(item)
            best = (ranked[0].get("visual") if ranked else {}) or {}
            return ranked, {
                "visual_ranked": bool(ranked),
                "visual_lock": bool(best.get("same_work") and best.get("match_clothes")),
                "compared": len(ranked),
                "note": "",
            }

        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=vision
        ), mock.patch.object(S, "ocr_image_bytes", side_effect=ocr), mock.patch.object(
            S, "run_identify_pipeline", side_effect=pipe
        ), mock.patch.object(S, "search_by_title", return_value=None), mock.patch.object(
            S, "rank_candidates_by_visual", side_effect=rank
        ), mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
        ):
            payload, status = S.run_multi_identify_pipeline(
                [(img_swim, "swim.jpg"), (img_phrase, "phrase.jpg"), (img_crop, "crop.jpg")]
            )
        self.assertEqual(status, 200)
        return payload

    def test_three_frames_are_stored_with_distinct_outcomes(self):
        payload = self._run_three()
        session = payload.get("identify_session") or {}
        self.assertTrue(session.get("id"))
        self.assertEqual(session.get("image_count"), 3)
        self.assertEqual(session.get("result_count"), 3)
        frames = session.get("frames") or []
        self.assertEqual([f.get("index") for f in frames], [1, 2, 3])
        codes = [f.get("final_code") for f in frames]
        self.assertEqual(codes, ["SWIM-804", "SHJG-448", "JULX-271"])
        self.assertEqual(len(set(codes)), 3)
        titles = [f.get("final_title") for f in frames]
        self.assertIn("媚薬", titles[0])
        self.assertIn("舌技が神", titles[1])
        self.assertTrue(titles[2])
        self.assertTrue(all(f.get("drop_reason") is None for f in frames))
        self.assertEqual(frames[0].get("ocr_title"), "巨乳水泳部員の媚薬合宿記録")
        self.assertIsNone(frames[0].get("vision_title"))
        self.assertEqual(frames[1].get("vision_title"), "舌技が神")
        self.assertEqual(frames[2].get("vision_code"), "JULX-271")
        self.assertTrue(frames[0].get("visual_lock"))
        self.assertFalse(frames[0].get("title_only"))
        self.assertTrue(str(frames[0].get("preview_ref") or "").startswith("sha256:"))
        self.assertEqual(frames[0].get("final_slot"), 1)
        self.assertEqual(frames[2].get("final_slot"), 3)
        banner = (session.get("banner") or "") + (session.get("summary") or "")
        self.assertIn("3", session.get("summary") or "")
        self.assertNotIn("略過", banner)
        self.assertNotIn("去重", banner)
        stored = S.identify_session_get(session["id"])
        self.assertEqual(stored.get("frames")[1].get("final_code"), "SHJG-448")

    def test_owner_token_required_for_list_and_detail(self):
        payload = self._run_three()
        sid = payload["session_id"]
        os.environ["OWNER_DEVICE_TOKEN"] = "test-owner-token"
        os.environ["SITE_PASSWORD"] = "guest-secret"
        client = S.app.test_client()

        blocked = client.get("/api/owner/identify-sessions")
        self.assertEqual(blocked.status_code, 401)
        self.assertFalse((blocked.get_json() or {}).get("ok"))

        wrong = client.get(
            "/api/owner/identify-sessions",
            headers={"Authorization": "Bearer not-the-token"},
        )
        self.assertEqual(wrong.status_code, 401)

        listed = client.get(
            "/api/owner/identify-sessions",
            headers={"Authorization": "Bearer test-owner-token"},
        )
        self.assertEqual(listed.status_code, 200)
        body = listed.get_json()
        self.assertTrue(body.get("ok"))
        rows = body.get("sessions") or []
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("id"), sid)
        self.assertEqual(row.get("image_count"), 3)
        self.assertIn("banner", row)
        self.assertIn("summary", row)
        self.assertEqual(len(row.get("frames") or []), 3)
        self.assertNotIn("fingerprint", row["frames"][0])
        self.assertEqual([f.get("final_code") for f in row["frames"]], ["SWIM-804", "SHJG-448", "JULX-271"])

        detail = client.get(
            "/api/owner/identify-sessions/" + sid,
            headers={"X-Owner-Token": "test-owner-token"},
        )
        self.assertEqual(detail.status_code, 200)
        session = (detail.get_json() or {}).get("session") or {}
        self.assertEqual(session.get("id"), sid)
        self.assertEqual(len(session.get("frames") or []), 3)
        self.assertTrue(str(session["frames"][0].get("preview_ref") or "").startswith("sha256:"))
        self.assertEqual(session["frames"][0].get("final_title"), "巨乳水泳部員の媚薬合宿記録")
        self.assertIn("vision_title", session["frames"][1])
        self.assertIn("visual_lock", session["frames"][2])

        missing = client.get(
            "/api/owner/identify-sessions/ses_missing",
            headers={"Authorization": "Bearer test-owner-token"},
        )
        self.assertEqual(missing.status_code, 404)

        with client.session_transaction() as sess:
            sess["site_ok"] = True
        via_login = client.get("/api/owner/identify-sessions")
        self.assertEqual(via_login.status_code, 200)


if __name__ == "__main__":
    unittest.main()
