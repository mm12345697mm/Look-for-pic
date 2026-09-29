#!/usr/bin/env python3
"""視覺相似 related bucket, and the catalog fetch reuse that feeds it and ↓."""
from __future__ import annotations

import io
import os
import random
import sys
import threading
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _smooth(w: int, h: int, seed: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (
                (x * 3 + seed) % 255,
                (y * 2 + seed * 2) % 255,
                ((x // 8) * 20 + (y // 8) * 15 + seed) % 255,
            )
    return img


def _scene(seed: int, *, bg=(40, 40, 60), fig=(220, 170, 150), box=(35, 30, 85, 150), bar=False) -> Image.Image:
    """A figure on a background with grain. Same layout + palette looks alike."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (120, 160), bg)
    draw = ImageDraw.Draw(img)
    draw.ellipse(box, fill=fig)
    if bar:
        draw.rectangle((0, 0, 120, 20), fill=(250, 250, 250))
    px = img.load()
    for y in range(160):
        for x in range(120):
            r, g, b = px[x, y]
            n = rnd.randint(-70, 70)
            px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    return img


def _upload_near_far() -> tuple[Image.Image, Image.Image, Image.Image]:
    upload = _scene(1, bar=True)
    near = _scene(2, bg=(60, 40, 40))
    far = _scene(3, bg=(200, 220, 90), fig=(20, 60, 160), box=(5, 90, 60, 150))
    return upload, near, far


def _flat(w: int, h: int, rgb: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (w, h), rgb)
    px = img.load()
    for y in range(0, h, 7):
        for x in range(w):
            px[x, y] = (255 - rgb[0], 255 - rgb[1], 255 - rgb[2])
    return img


def _row(code: str, *, line: str | None = None, why: str = "", visual=None, cover=True, **extra) -> dict:
    row = {
        "code": code,
        "title": f"{code} title",
        "cover": f"https://pics.dmm.co.jp/digital/video/{code.lower()}/{code.lower()}pl.jpg" if cover else None,
        "stills": [],
    }
    if line:
        row["line"] = line
    if why:
        row["why"] = why
    if visual is not None:
        row["visual"] = visual
    row.update(extra)
    return row


def _weak_payload(candidates=None, related=None) -> dict:
    return {
        "ok": True,
        "code": "MAIN-001",
        "title": "メイン作品",
        "actress": "",
        "visual_mismatch": True,
        "visual_lock": False,
        "visual_note": "未核對圖片（人物／衣服／姿勢與這張上傳圖不符）",
        "visual_meta": {"visual_ranked": True, "visual_lock": False},
        "candidates": list(candidates or []),
        "related_by_title": list(related or []),
    }


PARTIAL = {
    "same_work": False,
    "confidence": 0.55,
    "match_person": True,
    "match_clothes": False,
    "match_face": True,
    "match_pose": False,
}
STRANGER = {
    "same_work": False,
    "confidence": 0.20,
    "match_person": False,
    "match_clothes": False,
}


class TestVisualSimilarBucket(unittest.TestCase):
    def test_weak_card_gets_labelled_similar_rows_without_locking(self):
        payload = _weak_payload(
            candidates=[
                _row("MAIN-001", visual=PARTIAL),
                _row("LOOK-002", visual=PARTIAL),
                _row("FAR-003", visual=STRANGER),
            ]
        )
        out = S._rescue_weak_visual_related(payload)
        rel = out["related_by_title"]
        visual = [r for r in rel if r.get("line") == "visual"]
        self.assertEqual([r["code"] for r in visual], ["LOOK-002"])
        self.assertEqual(visual[0]["why"], S.RELATED_VISUAL_WHY)
        self.assertIn("未確認", visual[0]["why"])
        self.assertNotIn("title_zh", visual[0])
        self.assertNotIn("visual_lock", visual[0])
        self.assertFalse(any(r["code"] == "MAIN-001" for r in rel))
        self.assertFalse(any(r["code"] == "FAR-003" for r in rel))
        self.assertTrue(out["visual_mismatch"])
        self.assertFalse(out["visual_lock"])
        self.assertIn("未核對", out["visual_note"])

    def test_visual_bucket_is_capped_and_prefers_catalog_jackets(self):
        cands = [
            _row(f"LOOK-{i:03d}", visual=dict(PARTIAL, confidence=0.5 + i * 0.05), cover=(i != 5))
            for i in range(1, 6)
        ]
        out = S._rescue_weak_visual_related(_weak_payload(candidates=cands))
        visual = [r for r in out["related_by_title"] if r.get("line") == "visual"]
        self.assertEqual(len(visual), S.RELATED_VISUAL_CAP)
        self.assertEqual(visual[0]["code"], "LOOK-004")
        self.assertNotIn("LOOK-005", [r["code"] for r in visual])

    def test_locked_card_has_no_similar_bucket(self):
        payload = _weak_payload(candidates=[_row("LOOK-002", visual=PARTIAL)])
        payload["visual_mismatch"] = False
        payload["visual_lock"] = True
        payload["visual_note"] = ""
        payload["visual_meta"] = {"visual_ranked": True, "visual_lock": True}
        payload["related_by_title"] = [_row("SER-010", line="theme", why="同系列")]
        out = S._rescue_weak_visual_related(payload)
        self.assertEqual([r["code"] for r in out["related_by_title"]], ["SER-010"])

    def test_cap_keeps_visual_first_and_other_caps(self):
        rows = (
            [_row(f"VIS-{i:03d}", line="visual", why=S.RELATED_VISUAL_WHY) for i in range(1, 6)]
            + [_row(f"THM-{i:03d}", line="theme", why="片名相近") for i in range(1, 8)]
            + [_row(f"KWD-{i:03d}", line="keyword", why="關鍵字×1", keyword_hits=1) for i in range(1, 8)]
            + [_row(f"ACT-{i:03d}", line="actress", why="同演員") for i in range(1, 6)]
        )
        out = S._cap_related_buckets(rows)
        lines = [r["line"] for r in out]
        self.assertEqual(lines[:3], ["visual"] * 3)
        self.assertEqual(lines.count("visual"), 3)
        self.assertEqual(lines.count("theme"), 5)
        self.assertEqual(lines.count("keyword"), 5)
        self.assertEqual(lines.count("actress"), 3)
        slim = S._slim_related_for_cache(out)
        self.assertEqual(len(slim), 16)
        self.assertEqual(slim[0]["line"], "visual")

    def test_moved_row_returns_to_its_bucket_when_it_loses_the_slot(self):
        series = _row("MAIN-010", line="theme", why="同系列", look_alike=0.61)
        payload = _weak_payload(related=[series])
        out = S._rescue_weak_visual_related(payload)
        first = out["related_by_title"][0]
        self.assertEqual(first["line"], "visual")
        self.assertEqual(first["prior_line"], "theme")
        better = [_row(f"LOOK-{i:03d}", visual=dict(PARTIAL, confidence=0.9)) for i in range(1, 4)]
        again = S._rescue_weak_visual_related(
            dict(out, candidates=better, related_by_title=out["related_by_title"])
        )
        by_code = {r["code"]: r for r in again["related_by_title"]}
        self.assertEqual(by_code["MAIN-010"]["line"], "theme")
        self.assertEqual(by_code["MAIN-010"]["why"], "同系列")
        self.assertNotIn("prior_line", by_code["MAIN-010"])

    def test_explicit_person_and_clothes_miss_is_never_similar(self):
        row = _row("FAR-003", visual=STRANGER, look_alike=0.95)
        self.assertEqual(S._visual_similar_strength(row), 0.0)


class TestLookAlikeProbe(unittest.TestCase):
    def setUp(self):
        S._media_bytes_cache_clear()

    def test_probe_ranks_the_similar_jacket_into_the_bucket(self):
        upload_img, near, far = _upload_near_far()
        related = [
            _row("MAIN-011", line="theme", why="同系列"),
            _row("MAIN-012", line="theme", why="同系列"),
        ]
        blobs = {related[0]["cover"]: _png(near), related[1]["cover"]: _png(far)}
        payload = _weak_payload(related=related)
        with mock.patch.object(S, "download_cover_bytes", side_effect=lambda u, timeout=None: blobs.get(u)):
            out = S._probe_related_look_alike(payload, _png(upload_img))
        by_code = {r["code"]: r for r in out["related_by_title"]}
        self.assertGreaterEqual(by_code["MAIN-011"]["look_alike"], S.LOOK_ALIKE_MIN)
        self.assertEqual(by_code["MAIN-011"]["line"], "visual")
        self.assertEqual(by_code["MAIN-011"]["prior_line"], "theme")
        self.assertLess(by_code["MAIN-012"]["look_alike"], S.LOOK_ALIKE_MIN)
        self.assertEqual(by_code["MAIN-012"]["line"], "theme")
        self.assertEqual(out["related_by_title"][0]["code"], "MAIN-011")
        self.assertTrue(out["visual_mismatch"])
        self.assertFalse(out["visual_lock"])

    def test_probe_skips_locked_cards(self):
        payload = _weak_payload(related=[_row("NEAR-001", line="theme", why="片名相近")])
        payload.update(visual_lock=True, visual_mismatch=False, visual_note="")
        payload["visual_meta"] = {"visual_ranked": True, "visual_lock": True}
        dl = mock.Mock(return_value=None)
        with mock.patch.object(S, "download_cover_bytes", dl):
            S._probe_related_look_alike(payload, _png(_smooth(120, 160, 11)))
        dl.assert_not_called()

    def test_probe_stops_waiting_at_its_budget(self):
        release = threading.Event()

        def slow(url, timeout=None):
            release.wait(5)
            return None

        payload = _weak_payload(related=[_row("SLOW-001", line="theme", why="片名相近")])
        t0 = time.monotonic()
        with mock.patch.object(S, "download_cover_bytes", side_effect=slow):
            S._probe_related_look_alike(payload, _png(_smooth(120, 160, 11)), budget_s=0.5)
        elapsed = time.monotonic() - t0
        release.set()
        self.assertLess(elapsed, 2.5)


class TestMediaBytesCache(unittest.TestCase):
    URL = "https://pics.dmm.co.jp/digital/video/abc00001/abc00001pl.jpg"

    def setUp(self):
        S._media_bytes_cache_clear()

    def tearDown(self):
        S._media_bytes_cache_clear()

    def _resp(self, status=200, content=b"\xff\xd8" + b"q" * 2000):
        r = mock.Mock()
        r.status_code = status
        r.content = content
        r.url = self.URL
        r.headers = {"Content-Type": "image/jpeg"}
        return r

    def test_second_download_reuses_bytes(self):
        with mock.patch.object(S.requests, "get", return_value=self._resp()) as g:
            a = S.download_cover_bytes(self.URL)
            b = S.download_cover_bytes(self.URL)
        self.assertEqual(a, b)
        self.assertEqual(g.call_count, 1)

    def test_failure_is_not_cached(self):
        with mock.patch.object(S.requests, "get", return_value=self._resp(status=404, content=b"")) as g:
            self.assertIsNone(S.download_cover_bytes(self.URL))
        with mock.patch.object(S.requests, "get", return_value=self._resp()) as g2:
            self.assertIsNotNone(S.download_cover_bytes(self.URL))
        self.assertEqual(g.call_count, 1)
        self.assertEqual(g2.call_count, 1)

    def test_expired_entry_is_fetched_again(self):
        with mock.patch.object(S.requests, "get", return_value=self._resp()) as g:
            S.download_cover_bytes(self.URL)
            with mock.patch.object(S, "MEDIA_BYTES_CACHE_TTL_S", -1.0):
                S.download_cover_bytes(self.URL)
        self.assertEqual(g.call_count, 2)

    def test_cdn_proxy_serves_cached_bytes(self):
        with mock.patch.object(S.requests, "get", return_value=self._resp()) as g:
            S.download_cover_bytes(self.URL)
            client = S.app.test_client()
            from urllib.parse import quote

            r = client.get("/api/cdn-file?url=" + quote(self.URL))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(g.call_count, 1)

    def test_prefetch_runs_downloads_side_by_side(self):
        urls = [f"https://pics.dmm.co.jp/digital/video/s{i}/s{i}jp-1.jpg" for i in range(6)]
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def fake(url, timeout=None):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return b"blob-" + url.encode()

        with mock.patch.object(S, "download_cover_bytes", side_effect=fake):
            got = S._prefetch_media_bytes(urls + urls[:2])
        self.assertEqual(set(got), set(urls))
        self.assertGreater(active["peak"], 1)


class TestJobCatalogMemo(unittest.TestCase):
    def test_cover_cid_probed_once_per_job_and_again_outside(self):
        probe = mock.Mock(side_effect=lambda url, timeout=0: (True, url))
        with mock.patch.object(S, "probe_cover_url", probe):
            S.resolve_cover_cid("ABC-001")
            S.resolve_cover_cid("ABC-001")
            outside = probe.call_count
            S._job_zh_begin()
            try:
                S.resolve_cover_cid("ABC-001")
                S.resolve_cover_cid("abc-001")
            finally:
                S._job_zh_end()
        self.assertEqual(outside, 2)
        self.assertEqual(probe.call_count, 3)

    def test_cover_cid_miss_is_probed_again_in_the_same_job(self):
        probe = mock.Mock(return_value=(False, None))
        with mock.patch.object(S, "probe_cover_url", probe):
            S._job_zh_begin()
            try:
                S.resolve_cover_cid("ABC-001")
                first = probe.call_count
                S.resolve_cover_cid("ABC-001")
            finally:
                S._job_zh_end()
        self.assertEqual(probe.call_count, first * 2)

    def test_avbase_work_is_reused_as_a_copy(self):
        work = {"code": "ABC-001", "title": "作品", "genres": ["a"]}
        fetch = mock.Mock(return_value=dict(work))
        with mock.patch.object(S, "_fetch_avbase_by_code_uncached", fetch):
            S._job_zh_begin()
            try:
                a = S.fetch_avbase_by_code("ABC-001")
                a["genres"].append("mutated")
                b = S.fetch_avbase_by_code("ABC-001")
            finally:
                S._job_zh_end()
            S.fetch_avbase_by_code("ABC-001")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(b["genres"], ["a"])


class TestRankStampsLookAlike(unittest.TestCase):
    def setUp(self):
        S._media_bytes_cache_clear()

    def test_compared_covers_carry_a_look_alike_score(self):
        upload, near, far = _upload_near_far()
        cands = [
            _row("NEAR-001", score=0.8),
            _row("FAR-002", score=0.79),
        ]
        blobs = {cands[0]["cover"]: _png(near), cands[1]["cover"]: _png(far)}

        def fake_gemini(user, covers, key, timeout=10.0, labels=None):
            return [dict(PARTIAL) for _ in covers]

        with mock.patch.object(
            S, "download_cover_bytes", side_effect=lambda u, timeout=None: blobs.get(u)
        ), mock.patch.object(
            S, "sanitize_cover_fields", side_effect=lambda code=None, cid=None, cover=None: (cid, cover, [])
        ), mock.patch.object(S, "gemini_rank_covers_batch", side_effect=fake_gemini):
            ranked, meta = S.rank_candidates_by_visual(_png(upload), cands, api_key="k", budget_s=5)
        by_code = {r["code"]: r for r in ranked}
        self.assertIn("look_alike", by_code["NEAR-001"])
        self.assertIn("look_alike", by_code["FAR-002"])
        self.assertGreater(by_code["NEAR-001"]["look_alike"], by_code["FAR-002"]["look_alike"])
        self.assertFalse(meta.get("visual_lock"))


if __name__ == "__main__":
    unittest.main()
