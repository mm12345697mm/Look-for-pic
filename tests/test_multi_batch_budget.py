"""Large multi-image batches must finish or degrade per slot, not die as one 500.

The live failure was gunicorn's 180s sync worker wall during a 14-image
identify. Jacket crop-align itself is a small CPU cost; the batch has to
stop on its own clock and return the slots it finished.
"""

from __future__ import annotations

import io
import time
import unittest
from unittest import mock

from PIL import Image

import server as S


def _pattern(w: int, h: int, seed: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * seed + 20) % 255, (y * seed) % 255, ((x + y) * seed) % 255)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestJacketBudgetAndLock(unittest.TestCase):
    def test_series_front_still_locks_same_work_and_clothes(self):
        """#25 rule: crop-aligned jacket, same_work + match_clothes, no hardcoded codes."""
        front = _pattern(120, 160, 7)
        shared = "架空系列の同じ題名で巻だけ違う"
        blobs: dict[str, bytes] = {}
        cands = []
        # seed 7 is the upload. It is not the first catalog row.
        for i, seed in enumerate((3, 4, 5, 7, 8, 9), start=1):
            code = f"SER-{i:03d}"
            wide = Image.new("RGB", (400, 160), (20, 20, 20))
            wide.paste(_pattern(120, 160, seed), (280, 0))
            blobs[code] = _png(wide)
            cands.append(
                {
                    "code": code,
                    "title": shared,
                    "cover": f"https://pics.dmm.co.jp/digital/video/{code}/{code}pl.jpg",
                }
            )

        def fake_dl(url, timeout=None):
            for code, blob in blobs.items():
                if code in url:
                    return blob
            return None

        with mock.patch.object(S, "download_cover_bytes", side_effect=fake_dl):
            winner = S._jacket_lock_winner(_png(front), cands)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["code"], "SER-004")
        visual = winner["visual"]
        self.assertTrue(visual["same_work"])
        self.assertTrue(visual["match_clothes"])
        self.assertGreaterEqual(winner["jacket_score"], 0.70)
        self.assertGreaterEqual(winner["jacket_figure"], 0.58)

    def test_fourteen_by_eight_jacket_scores_stay_inside_budget(self):
        """14 uploads × 8 jackets must stay far under the old 180s worker wall."""
        users = [_pattern(120, 160, 11 + i) for i in range(14)]
        covers = []
        for j in range(8):
            wide = Image.new("RGB", (400, 160), (20, 20, 20))
            wide.paste(_pattern(120, 160, 30 + j), (280, 0))
            covers.append(wide)
        t0 = time.perf_counter()
        n = 0
        for user in users:
            for cover in covers:
                S._aligned_jacket_scores(user, cover)
                n += 1
        elapsed = time.perf_counter() - t0
        self.assertEqual(n, 14 * 8)
        # ~30ms each on this host; 20s is a wide ceiling so a slow CI still passes.
        self.assertLess(elapsed, 20.0, f"{n} jacket scores took {elapsed:.2f}s")
        print(f"jacket budget: {n} scores in {elapsed:.2f}s ({elapsed / n * 1000:.0f}ms each)")


class TestMultiBatchDeadline(unittest.TestCase):
    def _images(self, n: int) -> list[tuple[bytes, str]]:
        out = []
        for i in range(n):
            img = Image.new("RGB", (80, 80), (i * 15 % 255, 40, 80))
            out.append((_png(img), f"slot-{i + 1}.png"))
        return out

    def test_expired_deadline_returns_every_slot(self):
        images = self._images(14)
        with mock.patch.object(S, "ocr_image_bytes", side_effect=AssertionError("ocr")):
            with mock.patch.object(S, "get_gemini_api_key", return_value=""):
                payload, status = S.run_multi_identify_pipeline(
                    images,
                    deadline=time.monotonic() - 1,
                )
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("partial"))
        self.assertEqual(len(payload.get("results") or []), 14)
        self.assertTrue(all(r.get("timed_out") for r in payload["results"]))
        self.assertIn("不是查詢不到", payload.get("message") or "")
        self.assertNotIn("伺服器錯誤", payload.get("message") or "")

    def test_slow_slots_keep_finished_and_mark_the_rest(self):
        images = self._images(8)

        def slow_identify(**kwargs):
            time.sleep(0.7)
            return {
                "ok": True,
                "code": kwargs.get("user_code") or "ABP-123",
                "title": "架空題名のテスト",
            }, 200

        # Above the 8s vision gate, but not long enough to search every slot.
        deadline = time.monotonic() + 8.2
        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", return_value="ABP-123"):
                with mock.patch.object(S, "run_identify_pipeline", side_effect=slow_identify):
                    with mock.patch.object(
                        S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
                    ):
                        t0 = time.perf_counter()
                        payload, status = S.run_multi_identify_pipeline(
                            images, deadline=deadline
                        )
                        elapsed = time.perf_counter() - t0
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        results = payload.get("results") or []
        self.assertEqual(len(results), 8)
        done = [r for r in results if not r.get("timed_out")]
        waiting = [r for r in results if r.get("timed_out")]
        self.assertGreaterEqual(len(done), 1, payload.get("message"))
        self.assertGreaterEqual(len(waiting), 1, payload.get("message"))
        self.assertTrue(payload.get("partial"))
        self.assertIn("不是查詢不到", payload.get("message") or "")
        # Must return because of the deadline, not run all 8 × 0.45s plus margin.
        self.assertLess(elapsed, 8.0, elapsed)
        print(
            f"partial batch: {len(done)} finished, {len(waiting)} 尚未查完, {elapsed:.2f}s"
        )


class TestStreamKeepsProgress(unittest.TestCase):
    def test_stream_emits_keepalive_progress_and_result(self):
        png_a = _png(Image.new("RGB", (32, 32), (10, 20, 30)))
        png_b = _png(Image.new("RGB", (32, 32), (30, 20, 10)))

        def slow(images, **kwargs):
            time.sleep(0.35)
            on_progress = kwargs.get("on_progress")
            if on_progress:
                on_progress(
                    {
                        "step": "search",
                        "status": "active",
                        "detail": "搜尋第 1 張…",
                        "progress": 0.6,
                        "slot": {
                            "ok": True,
                            "code": "SER-001",
                            "title": "已完成的一張",
                            "from_image_index": 1,
                            "timed_out": False,
                        },
                    }
                )
            return (
                {
                    "ok": True,
                    "multi": True,
                    "results": [
                        {
                            "ok": True,
                            "code": "SER-001",
                            "title": "已完成的一張",
                            "from_image_index": 1,
                        }
                    ],
                    "message": "多圖辨識",
                },
                200,
            )

        with mock.patch.object(S, "run_multi_identify_pipeline", side_effect=slow):
            with mock.patch.object(S, "STREAM_KEEPALIVE_S", 0.05):
                client = S.app.test_client()
                res = client.post(
                    "/api/identify/stream",
                    data={
                        "images": [
                            (io.BytesIO(png_a), "a.png"),
                            (io.BytesIO(png_b), "b.png"),
                        ]
                    },
                    content_type="multipart/form-data",
                )
                body = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/event-stream", (res.content_type or ""))
        self.assertIn(": keepalive", body)
        self.assertIn("搜尋第 1 張", body)
        self.assertIn('"type": "result"', body)
        self.assertIn("SER-001", body)


class TestCoverCache(unittest.TestCase):
    def test_same_jacket_url_is_downloaded_once(self):
        S._COVER_BYTES_CACHE.clear()
        blob = b"\xff\xd8" + b"x" * 900
        calls = {"n": 0}

        class Resp:
            status_code = 200
            content = blob
            url = "https://pics.dmm.co.jp/digital/video/abc001/abc001pl.jpg"
            headers = {"Content-Type": "image/jpeg"}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            return Resp()

        url = "https://pics.dmm.co.jp/digital/video/abc001/abc001pl.jpg"
        try:
            with mock.patch.object(S.requests, "get", side_effect=fake_get):
                first = S.download_cover_bytes(url)
                second = S.download_cover_bytes(url)
        finally:
            S._COVER_BYTES_CACHE.clear()
        self.assertEqual(first, blob)
        self.assertEqual(second, blob)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
