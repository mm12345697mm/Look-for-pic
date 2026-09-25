#!/usr/bin/env python3
"""重新搜索 resolves one card's jacket without a new identify or an upload cover."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


DMM_COVER = "https://pics.dmm.co.jp/digital/video/ovvr00635/ovvr00635pl.jpg"
DMM_STILL = "https://pics.dmm.co.jp/digital/video/ovvr00635/ovvr00635jp-1.jpg"
MISSAV_COVER = "https://fourhoi.com/ovvr-635/preview.jpg"
UPLOAD = "data:image/jpeg;base64,qq"


class CoverRefreshTests(unittest.TestCase):
    def test_dmm_jacket_and_stills(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=("ovvr00635", DMM_COVER, [DMM_STILL])
        ), mock.patch.object(S, "fetch_public_zh_catalog") as public, mock.patch.object(
            S, "run_identify_pipeline", side_effect=AssertionError("identify must not run")
        ):
            out = S.refresh_catalog_cover("ovvr-635", title="痴女ヘブン")
        self.assertTrue(out["ok"])
        self.assertEqual(out["code"], "OVVR-635")
        self.assertEqual(out["cover"], DMM_COVER)
        self.assertEqual(out["cid"], "ovvr00635")
        self.assertEqual(out["stills"], [DMM_STILL])
        self.assertFalse(str(out["cover"]).startswith(("data:", "blob:")))
        public.assert_not_called()

    def test_missav_when_dmm_is_empty(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=(None, None, [])
        ), mock.patch.object(
            S,
            "fetch_public_zh_catalog",
            return_value={"cover": MISSAV_COVER, "cover_source": "missav"},
        ) as public:
            out = S.refresh_catalog_cover("OVVR-635", title="痴女ヘブン")
        public.assert_called_once()
        self.assertEqual(out["cover"], MISSAV_COVER)
        self.assertEqual(out["cover_source"], "missav")
        self.assertEqual(out["stills"], [])
        self.assertIsNone(out["cid"])

    def test_still_empty_when_every_source_misses(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=(None, None, [])
        ), mock.patch.object(S, "fetch_public_zh_catalog", return_value={}):
            out = S.refresh_catalog_cover("OVVR-635")
        self.assertTrue(out["ok"])
        self.assertIsNone(out["cover"])
        self.assertEqual(out["stills"], [])

    def test_upload_and_blob_are_not_jackets(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=(None, UPLOAD, ["blob:https://x/1"])
        ), mock.patch.object(
            S,
            "fetch_public_zh_catalog",
            return_value={"cover": "blob:https://missav.ai/1", "cover_source": "missav"},
        ):
            out = S.refresh_catalog_cover("OVVR-635")
        self.assertIsNone(out["cover"])
        self.assertEqual(out["stills"], [])
        self.assertFalse(str(out.get("cover") or "").startswith(("data:", "blob:")))

    def test_data_cover_falls_through_to_public_https(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=("ovvr00635", UPLOAD, [])
        ), mock.patch.object(
            S,
            "fetch_public_zh_catalog",
            return_value={"cover": MISSAV_COVER, "cover_source": "jable"},
        ):
            out = S.refresh_catalog_cover("OVVR-635")
        self.assertEqual(out["cover"], MISSAV_COVER)
        self.assertEqual(out["cover_source"], "jable")
        self.assertIsNone(out["cid"])

    def test_route_ignores_client_cover(self):
        with mock.patch.object(
            S, "sanitize_cover_fields", return_value=("ovvr00635", DMM_COVER, [DMM_STILL])
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=AssertionError("identify must not run")
        ):
            client = S.app.test_client()
            res = client.post(
                "/api/cover-refresh",
                json={
                    "code": "OVVR-635",
                    "title": "痴女ヘブン",
                    "cover": UPLOAD,
                    "cid": "ovvr00635",
                },
            )
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["cover"], DMM_COVER)
        self.assertNotIn("data:", str(body))
        self.assertEqual(body["stills"], [DMM_STILL])

    def test_missing_code(self):
        out = S.refresh_catalog_cover("", title="不是番號的句子")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["cover"])


if __name__ == "__main__":
    unittest.main()
