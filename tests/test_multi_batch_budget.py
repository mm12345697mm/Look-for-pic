"""Large multi-image batches finish every slot. A cut jacket still does not guess.

Jacket crop-align stays the #25 lock. A shared identify clock must not mark
a later frame 尚未查完, and a compare that was cut off must not keep a volume.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
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

    def test_expired_deadline_still_finishes_every_slot(self):
        """A shared identify clock must not mark later frames 尚未查完."""
        images = self._images(4)
        codes = ["ABP-101", "ABP-102", "ABP-103", "ABP-104"]

        def identify(**kwargs):
            return {
                "ok": True,
                "code": kwargs.get("user_code"),
                "title": "架空題名のテスト",
            }, 200

        events: list[dict] = []
        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", side_effect=codes):
                with mock.patch.object(S, "run_identify_pipeline", side_effect=identify):
                    with mock.patch.object(
                        S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
                    ):
                        payload, status = S.run_multi_identify_pipeline(
                            images,
                            deadline=time.monotonic() - 5,
                            on_progress=events.append,
                        )
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("partial"))
        results = payload.get("results") or []
        self.assertEqual(len(results), 4)
        self.assertEqual([r.get("code") for r in results], codes)
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        self.assertNotIn("時間不夠", payload.get("message") or "")
        phases = [e.get("phase") for e in events if e.get("phase")]
        for phase in ("辨識中", "目錄查詢", "封面鎖定", "相關作品"):
            self.assertIn(phase, phases, phases)

    def test_four_slots_are_not_skipped_to_save_related_time(self):
        """The old reserve stopped searching once a hit existed and <20s remained."""
        images = self._images(4)
        calls: list[str] = []

        def identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            calls.append(code)
            return {"ok": True, "code": code, "title": "架空題名のテスト"}, 200

        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", side_effect=["SER-221", "SER-222", "SER-223", "SER-224"]):
                with mock.patch.object(S, "run_identify_pipeline", side_effect=identify):
                    with mock.patch.object(
                        S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
                    ):
                        payload, status = S.run_multi_identify_pipeline(
                            images,
                            deadline=time.monotonic() + 8,
                        )
        self.assertEqual(status, 200)
        self.assertEqual(calls, ["SER-221", "SER-222", "SER-223", "SER-224"])
        results = payload.get("results") or []
        self.assertEqual(len(results), 4)
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        self.assertFalse(payload.get("partial"))


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
        self.assertIn('"type": "job"', body)
        self.assertIn('"type": "result"', body)
        self.assertIn("還在找", body)
        self.assertIn("SER-001", body)
        job_id = ""
        for chunk in body.split("\n\n"):
            line = next((ln[5:].strip() for ln in chunk.split("\n") if ln.startswith("data:")), "")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "job":
                job_id = str(evt.get("job_id") or "")
        self.assertTrue(job_id.startswith("job_"), body[:400])
        stored = S.identify_job_public(job_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("status"), "done")
        self.assertIn("SER-001", json.dumps(stored.get("result"), ensure_ascii=False))
        self.assertNotIn("查詢不到", stored.get("message") or "")


class TestIdentifyJobResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = S._IDENTIFY_JOBS_PATH
        S._IDENTIFY_JOBS_PATH = Path(self.tmp.name) / "identify-jobs.json"

    def tearDown(self):
        S._IDENTIFY_JOBS_PATH = self._prev
        self.tmp.cleanup()

    def test_running_job_reports_progress_without_a_guessed_code(self):
        job_id = S.identify_job_create(14)
        self.assertTrue(job_id)
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "done",
                "detail": "一張完成",
                "progress": 0.6,
                "slot": {
                    "ok": True,
                    "code": "ABP-123",
                    "title": "已完成的一張",
                    "from_image_index": 1,
                    "timed_out": False,
                    "image_bytes": b"not-for-the-client",
                },
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "done",
                "detail": "時間不夠",
                "progress": 0.7,
                "slot": {
                    "ok": True,
                    "code": "TITLE-SEARCH",
                    "title": "（這張尚未查完）",
                    "from_image_index": 2,
                    "timed_out": True,
                    "message": "請再上傳這張重查一次。不是查詢不到。",
                },
            },
        )
        view = S.identify_job_public(job_id)
        self.assertEqual(view["status"], "running")
        self.assertFalse(view["stale"])
        self.assertEqual(view["done_count"], 1)
        self.assertEqual(view["image_count"], 14)
        self.assertIn("還在找", view["message"])
        self.assertIn("已完成 1／共 14", view["message"])
        self.assertNotIn("查詢不到", view["message"])
        self.assertNotIn("未找到番號", view["message"])
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("not-for-the-client", blob)
        self.assertEqual(view["slots"][0]["code"], "ABP-123")
        self.assertTrue(view["slots"][1]["timed_out"])

    def test_quiet_job_is_stalled_not_a_miss(self):
        job_id = S.identify_job_create(8)
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "done",
                "slot": {
                    "ok": True,
                    "code": "SER-004",
                    "title": "鎖定的一張",
                    "from_image_index": 4,
                    "timed_out": False,
                    "visual_lock": True,
                },
            },
        )

        def age(job):
            job["updated_at"] = time.time() - (S.IDENTIFY_JOB_STALE_S + 5)

        S._identify_jobs_mutate(job_id, age)
        view = S.identify_job_public(job_id)
        self.assertEqual(view["status"], "stalled")
        self.assertTrue(view["stale"])
        self.assertIn("卡住或逾時可再補", view["message"])
        self.assertIn("已完成 1／共 8", view["message"])
        self.assertEqual(view["slots"][0]["code"], "SER-004")
        self.assertNotIn("result", view)
        self.assertNotIn("查詢不到", view["message"])

    def test_finish_keeps_the_pipeline_result(self):
        job_id = S.identify_job_create(2)
        S.identify_job_finish(
            job_id,
            {
                "ok": True,
                "multi": True,
                "partial": True,
                "image_count": 2,
                "results": [
                    {"ok": True, "code": "SER-004", "title": "鎖定", "from_image_index": 1},
                    {
                        "ok": True,
                        "code": "TITLE-SEARCH",
                        "title": "（這張尚未查完）",
                        "from_image_index": 2,
                        "timed_out": True,
                    },
                ],
                "message": "多圖辨識",
            },
            200,
        )
        view = S.identify_job_public(job_id)
        self.assertEqual(view["status"], "done")
        self.assertEqual(view["done_count"], 1)
        self.assertIn("其餘可再補", view["message"])
        self.assertEqual(view["result"]["results"][0]["code"], "SER-004")
        client = S.app.test_client()
        res = client.get("/api/identify/jobs/" + job_id)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["job"]["id"], job_id)
        missing = client.get("/api/identify/jobs/job_missing")
        self.assertEqual(missing.status_code, 404)


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


class TestDeadlineDoesNotInventAVolume(unittest.TestCase):
    def _series(self, seeds):
        shared = "架空系列の同じ題名で巻だけ違う"
        blobs = {}
        cands = []
        for i, seed in enumerate(seeds, start=1):
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
        front = _png(_pattern(120, 160, 7))
        return shared, blobs, cands, front

    def test_aborted_cover_does_not_lock_the_other_volumes(self):
        """The true jacket was not fetched. Do not lock on the covers that were."""
        _shared, blobs, cands, front = self._series((3, 4, 5, 7, 8, 9))

        def fetch(cand, deadline):
            code = cand["code"]
            if code == "SER-004":
                return cand, None, True
            return cand, blobs[code], False

        S._enter_batch_ctx(time.monotonic() + 60)
        try:
            with mock.patch.object(S, "_fetch_jacket_blob", side_effect=fetch):
                winner = S._jacket_lock_winner(front, cands)
            self.assertIsNone(winner)
            self.assertTrue(getattr(S._BATCH, "jacket_incomplete", False))
        finally:
            S._leave_batch_ctx()

    def test_past_deadline_does_not_lock_even_if_the_cover_matches(self):
        _shared, blobs, cands, front = self._series((3, 4, 5, 7, 8, 9))

        def fake_dl(url, timeout=None):
            self.fail(f"download started after the deadline: {url}")

        S._enter_batch_ctx(time.monotonic() - 1)
        try:
            with mock.patch.object(S, "download_cover_bytes", side_effect=fake_dl):
                winner = S._jacket_lock_winner(front, cands)
            self.assertIsNone(winner)
            self.assertTrue(getattr(S._BATCH, "jacket_incomplete", False))
        finally:
            S._leave_batch_ctx()

    def test_full_time_still_locks_same_work_and_clothes(self):
        """Thresholds stay 0.70 / 0.12 / 0.58 when the batch still has time."""
        _shared, blobs, cands, front = self._series((3, 4, 5, 7, 8, 9))

        def fake_dl(url, timeout=None):
            self.assertGreaterEqual(timeout or 0, S.COVER_DOWNLOAD_TIMEOUT)
            for code, blob in blobs.items():
                if code in url:
                    return blob
            return None

        S._enter_batch_ctx(time.monotonic() + 120)
        try:
            with mock.patch.object(S, "download_cover_bytes", side_effect=fake_dl):
                winner = S._jacket_lock_winner(front, cands)
            self.assertIsNotNone(winner)
            self.assertEqual(winner["code"], "SER-004")
            self.assertGreaterEqual(winner["jacket_score"], 0.70)
            self.assertGreaterEqual(winner["jacket_figure"], 0.58)
            self.assertFalse(getattr(S._BATCH, "jacket_incomplete", False))
        finally:
            S._leave_batch_ctx()

    def test_cut_short_series_is_retryable_not_the_catalog_first_row(self):
        shared, _blobs, cands, front = self._series((3, 4, 5, 7, 8, 9))
        other = _png(Image.new("RGB", (90, 90), (9, 9, 9)))

        def fake_lock(image, candidates):
            if getattr(S._BATCH, "active", False):
                S._BATCH.jacket_incomplete = True
            return None

        def fake_rank(*args, **kwargs):
            raise AssertionError("short visual rank must not run")

        def fake_identify(**kwargs):
            code = kwargs.get("user_code") or "SER-001"
            return {
                "ok": True,
                "code": code,
                "title": shared,
                "candidates": cands,
            }, 200

        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", return_value=shared):
                with mock.patch.object(S, "_jacket_lock_winner", side_effect=fake_lock):
                    with mock.patch.object(S, "rank_candidates_by_visual", side_effect=fake_rank):
                        with mock.patch.object(S, "run_identify_pipeline", side_effect=fake_identify):
                            with mock.patch.object(
                                S, "attach_related_by_title", side_effect=lambda result, **kwargs: result
                            ):
                                payload, status = S.run_multi_identify_pipeline(
                                    [(front, "a.png"), (other, "b.png")],
                                    deadline=time.monotonic() + 80,
                                )
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("partial"))
        results = payload.get("results") or []
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all(row.get("timed_out") for row in results), results)
        for row in results:
            self.assertNotEqual(row.get("code"), "SER-001")
            self.assertNotEqual(row.get("code"), "SER-004")
            self.assertIn("重查", row.get("message") or "")
            self.assertIn("不是查詢不到", row.get("message") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
