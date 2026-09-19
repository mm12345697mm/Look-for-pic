#!/usr/bin/env python3
"""Cover+stills zip for a single work (DMM CDN only)."""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

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
        self.assertTrue(
            S.allowed_media_url(
                "https://pics.dmm.com/digital/video/jufe00271/jufe00271pl.jpg"
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


class TestDownloadCoverBytes(unittest.TestCase):
    JUFE_PL = "https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg"
    JUFE_PS = "https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271ps.jpg"

    def test_variants_include_ps_and_mono_not_stills(self):
        urls = S.dmm_cover_variant_urls(self.JUFE_PL)
        self.assertEqual(urls[0], self.JUFE_PL)
        self.assertIn(self.JUFE_PS, urls)
        self.assertTrue(any("/mono/movie/adult/jufe00271/" in u for u in urls))
        self.assertTrue(
            any("pics.dmm.com/digital/video/jufe00271/" in u for u in urls),
            urls,
        )
        self.assertFalse(any("jp-" in u or "js-" in u for u in urls))

    def test_rejects_noimage_placeholder(self):
        jpeg = b"\xff\xd8" + b"x" * 3000
        with mock.patch.object(S.requests, "get") as g:
            r = mock.Mock()
            r.status_code = 200
            r.content = jpeg
            r.url = "https://pics.dmm.com/mono/noimage/movie/adult_ps.jpg"
            r.headers = {"Content-Type": "image/jpeg"}
            g.return_value = r
            self.assertIsNone(S.download_cover_bytes(self.JUFE_PS))

    def test_retries_alternate_referer_then_succeeds(self):
        jpeg = b"\xff\xd8" + b"y" * 2000
        bad = mock.Mock()
        bad.status_code = 403
        bad.content = b""
        bad.url = self.JUFE_PL
        bad.headers = {"Content-Type": "image/jpeg"}
        good = mock.Mock()
        good.status_code = 200
        good.content = jpeg
        good.url = self.JUFE_PL
        good.headers = {"Content-Type": "image/jpeg"}
        with mock.patch.object(S.requests, "get", side_effect=[bad, good]) as g:
            blob = S.download_cover_bytes(self.JUFE_PL, timeout=4.0)
        self.assertEqual(blob, jpeg)
        self.assertEqual(g.call_count, 2)

    def test_cdn_file_falls_back_to_ps(self):
        jpeg = b"\xff\xd8" + b"z" * 2000

        def fake(url, timeout=None):
            if str(url).endswith("pl.jpg"):
                return None
            if str(url).endswith("ps.jpg") and "digital/video" in str(url):
                return jpeg
            return None

        with mock.patch.object(S, "download_cover_bytes", side_effect=fake):
            client = S.app.test_client()
            r = client.get("/api/cdn-file?url=" + quote(self.JUFE_PL))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, jpeg)

    def test_now_printing_and_noimage_rejected_as_media(self):
        self.assertFalse(
            S.allowed_media_url(
                "https://pics.dmm.co.jp/mono/noimage/movie/adult_ps.jpg"
            )
        )
        self.assertTrue(S.is_now_printing_url("https://pics.dmm.com/mono/noimage/x.jpg"))


class TestIsMidaAndZipForeign(unittest.TestCase):
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
