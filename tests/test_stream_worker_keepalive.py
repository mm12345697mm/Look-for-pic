#!/usr/bin/env python3
"""A dropped identify stream must not die at gunicorn's silence timeout.

Production's start command is still `gunicorn --timeout 180` (the Railway
service setting overrides railway.toml's 2400). Sync workers call notify()
only between requests. With `-w 2` the child also has sibling Worker objects
whose temp fds were closed; notifying the first one does not reset the live
heartbeat, and the arbiter murders the stream inside queue.Queue.get.

The job thread has to keep writing slots after that SSE connection is gone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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
# What `describe-service` showed for the live web process after PR #30.
LIVE_RAILWAY_TIMEOUT_S = 180


class _Cfg:
    umask = 0
    worker_tmp_dir = None
    uid = os.geteuid()
    gid = os.getegid()
    max_requests = 0
    max_requests_jitter = 0


def _would_murder(worker, timeout: float) -> bool:
    """Match gunicorn's arbiter: murder when age is strictly greater than timeout."""
    age = S._gunicorn_silence_age(worker)
    if age is None:
        return False
    return age > float(timeout)


def _known_good_batch() -> tuple[dict, int]:
    results = []
    for code in ("MIDA-616", "APGH-012", "JUFE-271", "SSIS-001"):
        cid = code.lower().replace("-", "")
        results.append(
            {
                "ok": True,
                "code": code,
                "title": code,
                "cover": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg",
                "stills": [f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}jp-1.jpg"],
                "related_by_title": [],
            }
        )
    return {
        "ok": True,
        "code": "MIDA-616",
        "title": "MIDA-616",
        "results": results,
    }, 200


class TestLiveWorkerHeartbeat(unittest.TestCase):
    def setUp(self):
        from gunicorn.workers.base import Worker

        S._GUNICORN_WORKER = None
        S._GUNICORN_WORKER_MISSING = False
        self.cfg = _Cfg()
        self.sibling = Worker(0, os.getpid(), [], None, 1, self.cfg, None)
        self.live = Worker(1, os.getpid(), [], None, 1, self.cfg, None)
        self.sibling.pid = os.getpid() + 1
        self.live.pid = os.getpid()
        self.sibling.tmp.close()

    def tearDown(self):
        S._GUNICORN_WORKER = None
        S._GUNICORN_WORKER_MISSING = False
        for worker in (getattr(self, "live", None), getattr(self, "sibling", None)):
            if worker is None:
                continue
            try:
                worker.tmp.close()
            except Exception:
                pass

    def test_closed_sibling_is_not_the_heartbeat_target(self):
        picked = S._select_live_gunicorn_worker([self.sibling, self.live])
        self.assertIs(picked, self.live)
        with self.assertRaises(Exception):
            self.sibling.notify()

    def test_notify_resets_silence_age_past_a_short_timeout(self):
        self.live.notify()
        time.sleep(0.25)
        self.assertTrue(_would_murder(self.live, 0.2))
        # The old lookup cached whatever gc returned first and called notify()
        # on it. A closed sibling raises and leaves the live file stale.
        S._GUNICORN_WORKER = self.sibling
        S._notify_gunicorn_worker()
        self.assertIs(S._GUNICORN_WORKER, self.live)
        self.assertFalse(_would_murder(self.live, 0.2))
        self.assertLess(S._gunicorn_silence_age(self.live), 0.2)

    def test_heartbeat_fits_under_the_live_180s_command(self):
        self.assertLess(S.GUNICORN_HEARTBEAT_S * 4, LIVE_RAILWAY_TIMEOUT_S)
        self.assertLess(S.STREAM_KEEPALIVE_S * 4, LIVE_RAILWAY_TIMEOUT_S)
        self.assertEqual(S.GUNICORN_WORKER_TIMEOUT_S, 2400)
        self.assertEqual(S.SLOT_WORK_BUDGET_S, 600.0)

    def test_child_notify_is_visible_on_the_parent_heartbeat_file(self):
        script = r"""
import os, sys, time, traceback
sys.path.insert(0, %r)
import server as S
from gunicorn.workers.base import Worker

class Cfg:
    umask = 0
    worker_tmp_dir = None
    uid = os.geteuid()
    gid = os.getegid()
    max_requests = 0
    max_requests_jitter = 0

cfg = Cfg()
live = Worker(1, os.getpid(), [], None, 1, cfg, None)
sibling = Worker(0, os.getpid(), [], None, 1, cfg, None)
live.notify()
pid = os.fork()
if pid == 0:
    try:
        sibling.tmp.close()
        live.pid = os.getpid()
        sibling.pid = os.getpid() + 1
        S._GUNICORN_WORKER = sibling
        S._GUNICORN_WORKER_MISSING = False
        time.sleep(0.35)
        S._notify_gunicorn_worker()
        if S._GUNICORN_WORKER is not live:
            os._exit(2)
        os._exit(0)
    except Exception:
        traceback.print_exc()
        os._exit(3)
_child, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
if code != 0:
    os._exit(code)
age = S._gunicorn_silence_age(live)
if age is None or age > 0.25:
    print("stale-age", age)
    os._exit(4)
os._exit(0)
""" % (ROOT,)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            (proc.stdout or "") + (proc.stderr or ""),
        )


class TestIdentifyStreamSurvivesSilence(unittest.TestCase):
    def setUp(self):
        from gunicorn.workers.base import Worker

        self.tmp = tempfile.TemporaryDirectory()
        self.jobs_path = Path(self.tmp.name) / "jobs.json"
        self._prev_jobs = S._IDENTIFY_JOBS_PATH
        S._IDENTIFY_JOBS_PATH = self.jobs_path
        S._GUNICORN_WORKER = None
        S._GUNICORN_WORKER_MISSING = False
        self.cfg = _Cfg()
        self.sibling = Worker(0, os.getpid(), [], None, 1, self.cfg, None)
        self.live = Worker(1, os.getpid(), [], None, 1, self.cfg, None)
        self.sibling.pid = os.getpid() + 1
        self.live.pid = os.getpid()
        self.sibling.tmp.close()
        self.live.notify()

    def tearDown(self):
        S._GUNICORN_WORKER = None
        S._GUNICORN_WORKER_MISSING = False
        S._IDENTIFY_JOBS_PATH = self._prev_jobs
        for worker in (self.live, self.sibling):
            try:
                worker.tmp.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _images(self):
        return [(b"slot-%d" % i, "p%d.jpg" % i) for i in range(4)]

    def test_keepalive_notify_blocks_the_silence_kill_for_a_4_image_batch(self):
        payload, status = _known_good_batch()
        ages: list[float] = []
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(0.02):
                age = S._gunicorn_silence_age(self.live)
                if age is not None and age >= 0:
                    ages.append(age)

        def fake_multi(images, user_code="", user_title="", on_progress=None, job_id=None):
            self.assertEqual(len(images), 4)
            for i in range(4):
                S._progress(
                    on_progress,
                    "search",
                    "active",
                    f"搜尋第 {i + 1}/4 張…",
                    0.55 + (i * 0.05),
                    phase="目錄查詢",
                )
            time.sleep(0.7)
            S._progress(on_progress, "cover", "active", "抓取封面與劇照", 0.9, phase="封面鎖定")
            S._progress(on_progress, "done", "done", "完成，列出 4 部", 1.0)
            return payload, status

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        started = time.monotonic()
        try:
            with mock.patch.object(S, "STREAM_KEEPALIVE_S", 0.05), mock.patch.object(
                S, "collect_images_from_request", return_value=self._images()
            ), mock.patch.object(S, "run_multi_identify_pipeline", side_effect=fake_multi):
                client = S.app.test_client()
                res = client.post("/api/identify/stream", data={})
                # Streaming responses stay lazy until the body is read.
                body = res.get_data(as_text=True)
        finally:
            stop.set()
            watcher.join(timeout=1)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.55)
        self.assertGreaterEqual(len(ages), 5, ages)
        self.assertLess(max(ages), 0.3, ages)
        self.assertFalse(_would_murder(self.live, 0.3))
        self.assertIn(": keepalive", body)
        self.assertIn("搜尋第 4/4 張", body)
        self.assertIn("MIDA-616", body)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, body)
        self.assertEqual(res.status_code, 200)

    def test_closed_stream_job_still_advances_every_slot(self):
        payload, status = _known_good_batch()
        closed = threading.Event()
        job_box: dict = {}
        seen_slots: list[int] = []

        def fake_multi(images, user_code="", user_title="", on_progress=None, job_id=None):
            self.assertEqual(len(images), 4)
            for i in range(4):
                S._progress(
                    on_progress,
                    "search",
                    "active",
                    f"搜尋第 {i + 1}/4 張…",
                    0.55 + (i * 0.05),
                    phase="目錄查詢",
                )
                seen_slots.append(i + 1)
                if i == 0:
                    # Stay on slot 1 until the client has dropped SSE.
                    deadline = time.monotonic() + 2.0
                    while not closed.is_set() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(closed.is_set(), "stream reader never dropped")
                else:
                    time.sleep(0.04)
            S._progress(on_progress, "cover", "active", "抓取封面與劇照", 0.9, phase="封面鎖定")
            S._progress(on_progress, "done", "done", "完成，列出 4 部", 1.0)
            return payload, status

        def reader() -> None:
            try:
                with mock.patch.object(S, "STREAM_KEEPALIVE_S", 0.05), mock.patch.object(
                    S, "collect_images_from_request", return_value=self._images()
                ), mock.patch.object(S, "run_multi_identify_pipeline", side_effect=fake_multi):
                    client = S.app.test_client()
                    res = client.post("/api/identify/stream", data={}, buffered=False)
                    buf = b""
                    try:
                        for chunk in res.response:
                            if isinstance(chunk, str):
                                chunk = chunk.encode()
                            buf += chunk
                            text = buf.decode("utf-8", "replace")
                            found = re.search(r"job_[0-9a-f]{24}", text)
                            if found and "id" not in job_box:
                                job_box["id"] = found.group(0)
                            if "搜尋第 1/4" in text and job_box.get("id"):
                                # Pipeline is blocked on `closed` after this note.
                                job_box["early"] = S.identify_job_public(job_box["id"])
                                break
                    finally:
                        try:
                            res.close()
                        except Exception:
                            pass
            finally:
                closed.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(closed.wait(5), "reader did not close")
        early = job_box.get("early")
        job_id = job_box.get("id")
        self.assertTrue(job_id, job_box)
        self.assertIsNotNone(early, job_box)
        self.assertEqual(early.get("status"), "running")
        self.assertIn("搜尋第 1/4", str((early.get("progress") or {}).get("detail") or ""))

        view = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            view = S.identify_job_public(job_id)
            if view and view.get("status") == "done":
                break
            time.sleep(0.05)
        thread.join(timeout=3)
        self.assertEqual(seen_slots, [1, 2, 3, 4], seen_slots)
        self.assertIsNotNone(view)
        self.assertEqual(view.get("status"), "done", view)
        result = view.get("result") or {}
        self.assertEqual(len(result.get("results") or []), 4)
        covers = [str(row.get("cover") or "") for row in result["results"]]
        for cover in covers:
            self.assertTrue(cover.startswith("https://pics.dmm.co.jp/"), cover)
            self.assertFalse(cover.startswith("data:"))
        blob = json.dumps(view, ensure_ascii=False)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, blob)
        self.assertEqual(result.get("code"), "MIDA-616")

    def test_one_known_work_stays_on_the_single_lock_path(self):
        """MIDA-616 on one image already finishes on production #30.

        The silence fix is only for the 4-image stream. One upload must keep
        using run_identify_pipeline and return that work's catalog jacket
        with the visual lock still set. It must not enter 搜尋第 n/4.
        """
        jacket = "https://pics.dmm.co.jp/digital/video/mida616/mida616pl.jpg"
        still = "https://pics.dmm.co.jp/digital/video/mida616/mida616jp-1.jpg"
        calls = {"single": 0, "multi": 0}

        def fake_single(**kwargs):
            calls["single"] += 1
            self.assertEqual(kwargs.get("image_bytes"), b"mida-only")
            self.assertEqual(kwargs.get("filename"), "mida.jpg")
            return {
                "ok": True,
                "code": "MIDA-616",
                "title": "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク",
                "cover": jacket,
                "stills": [still],
                "visual_lock": True,
                "related_by_title": [],
            }, 200

        def fake_multi(*args, **kwargs):
            calls["multi"] += 1
            raise AssertionError("one image must not enter the multi-slot catalog loop")

        with mock.patch.object(
            S, "collect_images_from_request", return_value=[(b"mida-only", "mida.jpg")]
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_single
        ), mock.patch.object(
            S, "run_multi_identify_pipeline", side_effect=fake_multi
        ):
            client = S.app.test_client()
            res = client.post("/api/identify/stream", data={})
            body = res.get_data(as_text=True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(calls["single"], 1)
        self.assertEqual(calls["multi"], 0)
        self.assertIn("MIDA-616", body)
        self.assertIn(jacket, body)
        self.assertNotIn("搜尋第", body)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, body)
        found = re.search(r"job_[0-9a-f]{24}", body)
        self.assertIsNotNone(found)
        view = S.identify_job_public(found.group(0))
        self.assertEqual(view.get("status"), "done")
        result = view.get("result") or {}
        self.assertEqual(result.get("code"), "MIDA-616")
        self.assertTrue(result.get("visual_lock"))
        self.assertEqual(result.get("cover"), jacket)
        self.assertEqual(result.get("stills"), [still])
        self.assertFalse(str(result.get("cover")).startswith("data:"))
        self.assertNotIn("results", result)

    def test_job_progress_snapshot_does_not_rewind(self):
        job_id = S.identify_job_create(4)
        self.assertTrue(job_id)
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 3/4 張…",
                "progress": 0.68,
                "phase": "目錄查詢",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "封面鎖定第 3/4 張…",
                "progress": 0.7,
                "phase": "封面鎖定",
            },
        )
        # Next image may start catalog search again. That is forward.
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 4/4 張…",
                "progress": 0.74,
                "phase": "目錄查詢",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "封面鎖定第 4/4 張…",
                "progress": 0.8,
                "phase": "封面鎖定",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 3/4 張…",
                "progress": 0.68,
                "phase": "目錄查詢",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "vision",
                "status": "active",
                "detail": "第 2/4 張",
                "progress": 0.3,
                "phase": "辨識中",
            },
        )
        view = S.identify_job_public(job_id)
        progress = view.get("progress") or {}
        self.assertEqual(progress.get("detail"), "封面鎖定第 4/4 張…")
        self.assertEqual(progress.get("phase"), "封面鎖定")
        self.assertGreaterEqual(float(progress.get("progress") or 0), 0.8)
        self.assertEqual(view.get("status"), "running")
        blob = json.dumps(view, ensure_ascii=False)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, blob)
        S.identify_job_note(
            job_id,
            {"step": "done", "status": "done", "detail": "完成，列出 4 部", "progress": 1.0},
        )
        done = S.identify_job_public(job_id)
        self.assertEqual((done.get("progress") or {}).get("step"), "done")
        self.assertEqual((done.get("progress") or {}).get("detail"), "完成，列出 4 部")

    def test_job_progress_rejects_smaller_image_denominator(self):
        job_id = S.identify_job_create(7)
        self.assertTrue(job_id)
        live = 0.55 + 0.25 * (1 / 7)
        stale = 0.55 + 0.25 * (1 / 4)
        self.assertGreater(stale, live)
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 2/7 張…",
                "progress": live,
                "phase": "目錄查詢",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 2/4 張…",
                "progress": stale,
                "phase": "目錄查詢",
            },
        )
        S.identify_job_note(
            job_id,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 3/4 張…",
                "progress": 0.68,
                "phase": "目錄查詢",
            },
        )
        view = S.identify_job_public(job_id)
        progress = view.get("progress") or {}
        self.assertEqual(progress.get("detail"), "搜尋第 2/7 張…")
        self.assertAlmostEqual(float(progress.get("progress") or 0), live, places=6)
        self.assertEqual(view.get("image_count"), 7)
        # Related-work progress counts merged titles, not uploads.
        S.identify_job_note(
            job_id,
            {
                "step": "done",
                "status": "active",
                "detail": "相關作品 1/4…",
                "progress": 0.95,
                "phase": "相關作品",
            },
        )
        related = (S.identify_job_public(job_id).get("progress") or {})
        self.assertEqual(related.get("detail"), "相關作品 1/4…")
        fresh = S.identify_job_create(4)
        S.identify_job_note(
            fresh,
            {
                "step": "search",
                "status": "active",
                "detail": "搜尋第 2/4 張…",
                "progress": stale,
                "phase": "目錄查詢",
            },
        )
        self.assertEqual(
            (S.identify_job_public(fresh).get("progress") or {}).get("detail"),
            "搜尋第 2/4 張…",
        )


if __name__ == "__main__":
    unittest.main()
