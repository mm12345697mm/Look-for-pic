#!/usr/bin/env python3
"""重新辨識 must not replace a coded slot with an unlocked distant 品番."""
from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


TYVM_COVER = "https://pics.dmm.co.jp/digital/video/tyvm00349/tyvm00349pl.jpg"
APGH15_COVER = "https://pics.dmm.co.jp/digital/video/apgh00015/apgh00015pl.jpg"
APGH12_COVER = "https://pics.dmm.co.jp/digital/video/apgh00012/apgh00012pl.jpg"
JPEG = b"\xff\xd8\xff\xd9"


class ReidentifyAnchorTests(unittest.TestCase):
    def test_unlocked_distant_code_keeps_prior(self):
        out = S.anchor_reidentify_to_prior(
            {
                "ok": True,
                "code": "TYVM-349",
                "title": "清楚な女と思われがちだけど本当は",
                "cover": TYVM_COVER,
                "stills": [
                    "https://pics.dmm.co.jp/digital/video/tyvm00349/tyvm00349jp-1.jpg"
                ],
                "visual_lock": False,
                "visual_meta": {"visual_ranked": True, "visual_lock": False},
                "user_preview": "data:image/jpeg;base64,qq",
            },
            "APGH-015",
            JPEG,
        )
        self.assertEqual(out.get("code"), "APGH-015")
        self.assertTrue(out.get("visual_mismatch"))
        self.assertIn("未核對圖片", out.get("visual_note") or "")
        self.assertNotEqual(out.get("cover"), TYVM_COVER)
        self.assertFalse(str(out.get("cover") or "").startswith(("data:", "blob:")))
        self.assertEqual(out.get("reidentify_rejected_code"), "TYVM-349")
        self.assertFalse(out.get("visual_lock"))
        blob = str(out.get("title") or "") + str(out.get("title_zh") or "")
        self.assertNotIn("TYVM", blob)

    def test_same_code_without_lock_keeps_jacket_and_marks_unverified(self):
        out = S.anchor_reidentify_to_prior(
            {
                "ok": True,
                "code": "APGH-015",
                "title": "先生が2人っきりのプライベート補習",
                "cover": APGH15_COVER,
                "visual_lock": False,
                "visual_meta": {"visual_lock": False},
            },
            "APGH-015",
            JPEG,
        )
        self.assertEqual(out.get("code"), "APGH-015")
        self.assertEqual(out.get("cover"), APGH15_COVER)
        self.assertTrue(out.get("visual_mismatch"))
        self.assertIn("未核對圖片", out.get("visual_note") or "")

    def test_locked_same_label_sibling_is_accepted(self):
        out = S.anchor_reidentify_to_prior(
            {
                "ok": True,
                "code": "APGH-012",
                "title": "隣の巻",
                "cover": APGH12_COVER,
                "visual_lock": True,
                "visual_meta": {"visual_lock": True},
            },
            "APGH-015",
            JPEG,
        )
        self.assertEqual(out.get("code"), "APGH-012")
        self.assertEqual(out.get("cover"), APGH12_COVER)
        self.assertTrue(out.get("visual_lock"))
        self.assertFalse(out.get("visual_mismatch"))

    def test_locked_same_label_candidate_beats_unlocked_distant_top(self):
        out = S.anchor_reidentify_to_prior(
            {
                "ok": True,
                "code": "TYVM-349",
                "title": "別系列",
                "cover": TYVM_COVER,
                "visual_lock": False,
                "candidates": [
                    {
                        "code": "APGH-012",
                        "title": "隣の巻",
                        "cover": APGH12_COVER,
                        "visual": {"same_work": True, "match_clothes": True},
                    }
                ],
            },
            "APGH-015",
            JPEG,
        )
        self.assertEqual(out.get("code"), "APGH-012")
        self.assertEqual(out.get("cover"), APGH12_COVER)
        self.assertNotIn("tyvm", str(out.get("cover") or "").lower())

    def test_route_does_not_crown_unlocked_distant_hit(self):
        def fake(**kwargs):
            self.assertTrue(kwargs.get("image_bytes"))
            self.assertFalse(kwargs.get("user_code"))
            return {
                "ok": True,
                "code": "TYVM-349",
                "title": "別の作品",
                "cover": TYVM_COVER,
                "visual_lock": False,
                "visual_meta": {"visual_ranked": True, "visual_lock": False},
            }, 200

        with mock.patch.object(S, "run_identify_pipeline", side_effect=fake):
            client = S.app.test_client()
            res = client.post(
                "/api/identify",
                data={
                    "prior_code": "APGH-015",
                    "image": (io.BytesIO(JPEG), "shot.jpg"),
                },
            )
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body.get("code"), "APGH-015")
        self.assertTrue(body.get("visual_mismatch"))
        self.assertNotEqual(body.get("cover"), TYVM_COVER)
        self.assertNotIn("data:", str(body))
        self.assertNotIn("blob:", str(body))


if __name__ == "__main__":
    unittest.main()
