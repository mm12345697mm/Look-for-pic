#!/usr/bin/env python3
"""Cover+stills zip for a single work (DMM CDN only)."""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402


class TestAllowedMediaUrl(unittest.TestCase):
    def test_dmm_cdn_ok(self):
        self.assertTrue(
            S.allowed_media_url(
                "https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg"
            )
        )

    def test_rejects_other_hosts_and_now_printing(self):
        self.assertFalse(S.allowed_media_url("https://evil.example/x.jpg"))
        self.assertFalse(S.allowed_media_url("javascript:alert(1)"))
        self.assertFalse(
            S.allowed_media_url(
                "https://pics.dmm.co.jp/digital/video/aaa/now_printing.jpg"
            )
        )


class TestBuildWorkZip(unittest.TestCase):
    def test_zips_cover_and_stills_skips_foreign(self):
        def fake_fetch(url, timeout=None):
            return b"JPEGDATA" * 80

        data, n, fname = S.build_work_zip_bytes(
            "AAA-001",
            "https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg",
            [
                "https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001jp-1.jpg",
                "https://evil.example/nope.jpg",
            ],
            fetch_bytes=fake_fetch,
        )
        self.assertEqual(n, 2)
        self.assertEqual(fname, "AAA-001.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
        self.assertEqual(len(names), 2)
        self.assertTrue(all(name.startswith("AAA-001/") for name in names))
        self.assertIn("AAA-001/cover.jpg", names)
        self.assertIn("AAA-001/still-01.jpg", names)

    def test_is_mida616(self):
        self.assertTrue(S.is_mida616("MIDA-616"))
        self.assertTrue(S.is_mida616("mida616"))
        self.assertFalse(S.is_mida616("MIDA-617"))
        self.assertFalse(S.is_mida616(""))
        data, n, fname = S.build_work_zip_bytes(
            "BBB-002",
            "https://example.com/cover.jpg",
            [],
            fetch_bytes=lambda *a, **k: b"x" * 100,
        )
        self.assertEqual(n, 0)
        self.assertEqual(fname, "BBB-002.zip")
        self.assertTrue(isinstance(data, (bytes, bytearray)))


if __name__ == "__main__":
    unittest.main()
