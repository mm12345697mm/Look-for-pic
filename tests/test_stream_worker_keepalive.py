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

        def fake_multi(images, user_code="", user_title="", on_progress=None):
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

        def fake_multi(images, user_code="", user_title="", on_progress=None):
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


if __name__ == "__main__":
    unittest.main()
