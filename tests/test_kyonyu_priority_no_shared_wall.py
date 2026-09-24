#!/usr/bin/env python3
"""#25 identify stays the floor: no shared time wall, and 巨乳 hits stay.

A multi batch must finish vision → catalog → jacket lock for every slot.
Related runs after that, on its own pass. A shared identify budget must not
mark a later slot 時間不夠 / 尚未查完 / 尚未鎖定.

When the title says 巨乳, chips and the keyword bucket keep those hits.
A weak or coverless title does not take their place. Related covers are
catalog jacket URLs, never the upload bytes.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


SWIM = "巨乳水泳部員の媚薬合宿記録"
MIDA = "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク"
WALL_CONSTANTS = (
    "MULTI_IDENTIFY_BUDGET_S",
    "MULTI_SMALL_IDENTIFY_BUDGET_S",
    "MULTI_SLOT_RESERVE_S",
    "MULTI_VISION_START_S",
    "MULTI_SMALL_VISUAL_START_S",
    "MULTI_SMALL_BATCH_N",
)
FALSE_TIMEOUT = ("時間不夠", "尚未查完", "尚未鎖定")


def _png(tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag


class TestNoSharedIdentifyWall(unittest.TestCase):
    def test_wall_constants_are_absent(self):
        for name in WALL_CONSTANTS:
            self.assertFalse(hasattr(S, name), name)

    def test_multi_batch_finishes_every_slot_before_related(self):
        slots = [
            ("AAA-001", SWIM),
            ("BBB-002", "家庭教師の肉欲教育"),
            ("CCC-003", "満員電車の痴漢"),
            ("DDD-004", "温泉旅行の人妻"),
        ]
        titles = {code: title for code, title in slots}
        phases: list[tuple] = []
        upload = "data:image/jpeg;base64,QUJD"

        def fake_vision(image_bytes, mime, api_key):
            i = sum(1 for kind, *_ in phases if kind == "vision")
            code, title = slots[i]
            phases.append(("vision", code))
            return {"code": code, "title": title, "actress": "誰か"}

        def fake_identify(**kwargs):
            code = str(kwargs.get("user_code") or "")
            phases.append(("identify", code))
            cid = code.lower().replace("-", "")
            return {
                "ok": True,
                "code": code,
                "title": titles[code],
                "actress": "誰か",
                "cid": cid,
                "cover": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg",
                "stills": [f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}jp-1.jpg"],
            }, 200

        def fake_verify(hit, *args, **kwargs):
            phases.append(("lock", hit.get("code")))
            return hit

        budgets = []

        def fake_related(result, **kwargs):
            budgets.append(float(kwargs.get("budget_sec") or 0))
            phases.append(("related", result.get("code")))
            out = dict(result)
            out["related_by_title"] = [
                {
                    "code": "REL-101",
                    "title": "巨乳だけの合宿",
                    "line": "keyword",
                    "cover": "https://pics.dmm.co.jp/digital/video/rel00101/rel00101pl.jpg",
                    "stills": [
                        "https://pics.dmm.co.jp/digital/video/rel00101/rel00101jp-1.jpg"
                    ],
                    "matched_keywords": ["巨乳"],
                },
                {
                    "code": "REL-BAD",
                    "title": "這張上傳圖",
                    "line": "keyword",
                    "cover": upload,
                    "cover_url": upload,
                    "stills": [upload],
                    "user_preview": upload,
                    "image_bytes": b"\xff\xd8\xff",
                },
            ]
            return out

        images = [(_png(bytes([i])), f"slot{i}.png") for i in range(len(slots))]
        kwargs = {}
        if "deadline" in inspect.signature(S.run_multi_identify_pipeline).parameters:
            kwargs["deadline"] = time.monotonic() - 30
        step_phases: list[str] = []

        def on_progress(evt):
            if isinstance(evt, dict) and evt.get("phase"):
                step_phases.append(str(evt["phase"]))

        kwargs["on_progress"] = on_progress
        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_identify
        ), mock.patch.object(
            S, "verify_work_against_image", side_effect=fake_verify
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=fake_related
        ), mock.patch.object(S, "offline_cache_put", return_value=None):
            payload, status = S.run_multi_identify_pipeline(images, **kwargs)

        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 4, payload.get("message"))
        blob = json.dumps(payload, ensure_ascii=False)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, blob)
        for row, (code, title) in zip(results, slots):
            self.assertFalse(row.get("timed_out"))
            self.assertEqual(row.get("code"), code)
            self.assertIn("巨乳" if code == "AAA-001" else title[:2], row.get("title") or "")
            self.assertTrue(str(row.get("cover") or "").startswith("https://"))

        kinds = [item[0] for item in phases]
        self.assertEqual(kinds.count("vision"), 4)
        self.assertEqual(kinds.count("identify"), 4)
        self.assertEqual(kinds.count("lock"), 4)
        self.assertEqual(kinds.count("related"), 4)
        self.assertLess(kinds.index("vision"), kinds.index("identify"))
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "lock"), kinds.index("related"))
        for label in ("辨識中", "目錄查詢", "封面鎖定", "相關作品"):
            self.assertIn(label, step_phases, step_phases)
        self.assertLess(step_phases.index("辨識中"), step_phases.index("目錄查詢"))
        self.assertLess(step_phases.index("目錄查詢"), step_phases.index("封面鎖定"))
        self.assertLess(step_phases.index("封面鎖定"), step_phases.index("相關作品"))
        self.assertEqual(len(budgets), 4, budgets)
        for b in budgets:
            self.assertGreaterEqual(b, 500.0, budgets)
        self.assertEqual(S.SLOT_WORK_BUDGET_S, 600.0)
        self.assertEqual(S.GUNICORN_WORKER_TIMEOUT_S, 2400)

        def walk(rows):
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                cover = str(row.get("cover") or "")
                self.assertFalse(cover.startswith("data:"), cover)
                self.assertNotIn("QUJD", cover)
                self.assertFalse(row.get("user_preview"))
                self.assertNotIn("image_bytes", row)
                for u in row.get("stills") or []:
                    self.assertTrue(str(u).startswith("https://"), u)
                    self.assertNotIn("QUJD", str(u))

        for row in results:
            rel = row.get("related_by_title") or []
            self.assertTrue(rel, row.get("code"))
            covers = [str(r.get("cover") or "") for r in rel]
            self.assertIn(
                "https://pics.dmm.co.jp/digital/video/rel00101/rel00101pl.jpg",
                covers,
            )
            self.assertNotIn(upload, covers)
            walk(rel)
        walk(payload.get("related_by_title") or [])

    def test_successful_identify_cover_is_catalog_jacket_not_the_upload(self):
        """The query image must not become the main work's cover or stills."""
        upload = "data:image/jpeg;base64,QUJD"
        code, title = "CAMP-100", SWIM
        cid = "camp100"
        jacket = f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg"

        def fake_vision(image_bytes, mime, api_key):
            return {"code": code, "title": title, "actress": "誰か"}

        def fake_identify(**kwargs):
            return {
                "ok": True,
                "code": code,
                "title": title,
                "actress": "誰か",
                "cid": cid,
                "cover": upload,
                "stills": [upload],
                "user_preview": upload,
            }, 200

        def fake_verify(hit, *args, **kwargs):
            return hit

        def fake_related(result, **kwargs):
            out = dict(result)
            out["related_by_title"] = []
            return out

        images = [(_png(b"\x01"), "a.png"), (_png(b"\x02"), "b.png")]
        with mock.patch.object(S, "get_gemini_api_key", return_value="test-key"), mock.patch.object(
            S, "call_gemini_vision", side_effect=fake_vision
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=fake_identify
        ), mock.patch.object(
            S, "verify_work_against_image", side_effect=fake_verify
        ), mock.patch.object(
            S, "attach_related_by_title", side_effect=fake_related
        ), mock.patch.object(S, "offline_cache_put", return_value=None):
            payload, status = S.run_multi_identify_pipeline(images)

        self.assertEqual(status, 200)
        self.assertEqual(payload.get("cover"), jacket)
        self.assertNotEqual(payload.get("cover"), upload)
        self.assertNotIn("QUJD", str(payload.get("cover") or ""))
        for row in payload.get("results") or []:
            self.assertEqual(row.get("cover"), jacket)
            self.assertNotEqual(row.get("cover"), row.get("user_preview"))
            for u in row.get("stills") or []:
                self.assertTrue(str(u).startswith("https://pics.dmm.co.jp/"), u)
                self.assertNotIn("QUJD", str(u))
                self.assertNotEqual(u, upload)

        kept = S._lock_work_catalog_media(
            {
                "code": code,
                "cid": cid,
                "cover": jacket,
                "stills": [f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}jp-1.jpg"],
                "user_preview": upload,
            }
        )
        self.assertEqual(kept["cover"], jacket)
        self.assertEqual(kept["user_preview"], upload)
        self.assertNotEqual(kept["cover"], kept["user_preview"])

        cleared = S._lock_work_catalog_media(
            {"code": code, "cover": upload, "stills": [upload], "user_preview": upload}
        )
        self.assertFalse(cleared.get("cover"))
        self.assertEqual(cleared.get("stills"), [])


class TestKyonyuKeywordPriority(unittest.TestCase):
    def test_swim_title_keeps_kyonyu_chip_and_stays_searchable(self):
        kws = S._extract_title_theme_keywords(SWIM)
        self.assertIn("巨乳", kws, kws)
        self.assertNotIn("巨肝", "".join(kws))
        self.assertTrue(S.is_usable_title(SWIM))
        norm = S.normalize_ocr_title(SWIM)
        self.assertIn("巨乳", norm)
        self.assertNotIn("巨肝", norm)
        qs = S._keyword_search_queries(SWIM, kws)
        self.assertTrue(qs)
        self.assertIn("巨乳", qs)
        self.assertFalse(any("巨肝" in q for q in qs))
        plain = S._extract_title_theme_keywords("媚薬の合宿記録")
        self.assertNotIn("巨乳", plain)
        self.assertIn("合宿", plain)
        self.assertIn("媚薬", plain)
        reject = getattr(S, "_title_has_ocr_garbage", None)
        if reject is not None:
            self.assertFalse(reject(SWIM))
            self.assertFalse(reject("巨乳水泳部員"))

    def test_swim_camp_emits_swim_and_camp_chips_and_keeps_kyonyu(self):
        """水泳部 and 合宿 are chips beside 巨乳 and 媚薬. Compounds still lead."""
        titled = "巨乳水泳部員 媚薬漬けレ●プ合宿"
        kws = S._extract_title_theme_keywords(titled)
        for tok in ("巨乳", "媚薬", "水泳部", "合宿"):
            self.assertIn(tok, kws, kws)
        self.assertNotIn("巨肝", "".join(kws))
        self.assertNotIn("水泳部員", kws)
        qs = S._keyword_search_queries(titled, kws)
        for tok in ("巨乳", "媚薬", "水泳部", "合宿"):
            self.assertIn(tok, qs, qs)
        wear = S._extract_title_theme_keywords("巨乳の水着")
        self.assertIn("巨乳", wear)
        self.assertIn("水着", wear)
        self.assertNotIn("合宿", wear)
        school = S._extract_title_theme_keywords("スクール水着の巨乳")
        self.assertIn("スクール水着", school, school)
        self.assertIn("巨乳", school)
        trad = S._extract_title_theme_keywords("巨乳媚藥水泳部合宿")
        self.assertIn("媚藥", trad, trad)
        self.assertIn("水泳部", trad)
        self.assertIn("合宿", trad)
        self.assertIn("巨乳", trad)
        no_camp = S._extract_title_theme_keywords("巨乳の媚薬")
        self.assertIn("巨乳", no_camp)
        self.assertIn("媚薬", no_camp)
        self.assertNotIn("合宿", no_camp)
        self.assertNotIn("水泳部", no_camp)
        mida = S._extract_title_theme_keywords(MIDA)
        self.assertNotIn("ノーブラ誘惑", mida)
        self.assertEqual(mida[0], "ノーブラ", mida)
        self.assertEqual(mida[1], "巨乳", mida)
        self.assertIn("誘惑", mida)
        self.assertLess(mida.index("巨乳"), mida.index("誘惑"))

    def test_swim_camp_keyword_bucket_mixes_kyonyu_and_requires_https(self):
        """巨乳+媚薬 and 巨乳+合宿/水泳部 lead. 媚薬+合宿 alone does not fill the cap.

        A coverless or data-URL row never enters 關鍵字相關 for this title.
        """
        title = "巨乳水泳部員 媚薬漬けレ●プ合宿"
        catalog = [
            {
                "code": "DRUG-001",
                "title": "媚薬合宿の記録",
                "score": 9,
                "cover": "https://pics.dmm.co.jp/digital/video/drug001/drug001pl.jpg",
            },
            {
                "code": "DRUG-002",
                "title": "媚薬の合宿水泳部",
                "score": 8,
                "cover": "https://pics.dmm.co.jp/digital/video/drug002/drug002pl.jpg",
            },
            {
                "code": "DRUG-003",
                "title": "合宿で媚薬",
                "score": 7,
                "cover": "https://pics.dmm.co.jp/digital/video/drug003/drug003pl.jpg",
            },
            {
                "code": "DRUG-004",
                "title": "水泳部の媚薬合宿",
                "score": 6,
                "cover": "https://pics.dmm.co.jp/digital/video/drug004/drug004pl.jpg",
            },
            {
                "code": "DRUG-005",
                "title": "媚薬合宿",
                "score": 5,
                "cover": "https://pics.dmm.co.jp/digital/video/drug005/drug005pl.jpg",
            },
            {
                "code": "BODY-101",
                "title": "巨乳の媚薬記録",
                "score": 0.2,
                "cover": "https://pics.dmm.co.jp/digital/video/body101/body101pl.jpg",
            },
            {
                "code": "BODY-102",
                "title": "巨乳の合宿記録",
                "score": 0.2,
                "cover": "https://pics.dmm.co.jp/digital/video/body102/body102pl.jpg",
            },
            {
                "code": "BODY-103",
                "title": "巨乳水泳部の記録",
                "score": 0.2,
                "cover": "https://pics.dmm.co.jp/digital/video/body103/body103pl.jpg",
            },
            {"code": "BODY-104", "title": "巨乳の媚薬合宿", "score": 4, "cover": ""},
            {
                "code": "BODY-105",
                "title": "巨乳媚薬の合宿",
                "score": 3,
                "cover": "data:image/jpeg;base64,QUJD",
            },
        ]
        seen: list[str] = []
        spent = {"on": False}

        def fake_fetch(q, actress=None):
            seen.append(q)
            spent["on"] = True
            return [dict(row) for row in catalog]

        def mono():
            return 1000.0 if spent["on"] else 0.0

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_fetch), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(
            S, "enrich_title_candidate", side_effect=fake_enrich
        ), mock.patch("time.monotonic", side_effect=mono):
            rows = S._find_related_by_keywords(title, max_n=5, budget_sec=6.0)

        for tok in ("巨乳", "水泳部", "合宿", "媚薬"):
            self.assertIn(tok, seen, seen)
        codes = [r["code"] for r in rows]
        self.assertNotIn("BODY-104", codes, codes)
        self.assertNotIn("BODY-105", codes, codes)
        self.assertIn("BODY-101", codes, codes)
        self.assertTrue("BODY-102" in codes or "BODY-103" in codes, codes)
        partners = set()
        for row in rows:
            cover = str(row.get("cover") or "")
            self.assertTrue(cover.startswith("https://"), cover)
            self.assertNotIn("QUJD", cover)
            matched = row.get("matched_keywords") or []
            if "巨乳" not in matched:
                continue
            if "媚薬" in matched or "媚藥" in matched:
                partners.add("drug")
            if "合宿" in matched or "水泳部" in matched:
                partners.add("camp")
        self.assertIn("drug", partners, rows)
        self.assertIn("camp", partners, rows)
        self.assertTrue(any("巨乳" in (r.get("matched_keywords") or []) for r in rows))
        first_body = min(
            i for i, r in enumerate(rows) if "巨乳" in (r.get("matched_keywords") or [])
        )
        drug_only = [
            i
            for i, r in enumerate(rows)
            if "巨乳" not in (r.get("matched_keywords") or [])
        ]
        if drug_only:
            self.assertLess(first_body, drug_only[0], codes)

    def test_kyonyu_hits_beat_weak_and_coverless_rows(self):
        catalog = [
            {"code": "WEAK-001", "title": "ノーブラ誘惑とナマ乳沼", "score": 9, "cover": ""},
            {
                "code": "WEAK-002",
                "title": "ノーブラ誘惑のナマ乳沼記録",
                "score": 8,
                "cover": "https://pics.dmm.co.jp/digital/video/weak002/weak002pl.jpg",
            },
            {"code": "WEAK-003", "title": "ナマ乳沼のノーブラ誘惑", "score": 7, "cover": ""},
            {"code": "WEAK-004", "title": "ノーブラ誘惑なナマ乳沼", "score": 6, "cover": ""},
            {"code": "WEAK-005", "title": "ナマ乳沼でノーブラ誘惑", "score": 5, "cover": ""},
            {
                "code": "BODY-101",
                "title": "ノーブラ誘惑の巨乳",
                "score": 0.3,
                "cover": "https://pics.dmm.co.jp/digital/video/body101/body101pl.jpg",
            },
            {
                "code": "BODY-202",
                "title": "巨乳のナマ乳沼",
                "score": 0.2,
                "cover": "https://pics.dmm.co.jp/digital/video/body202/body202pl.jpg",
            },
            {
                "code": "BODY-000",
                "title": "巨乳とナマ乳沼のメモ",
                "score": 4,
                "cover": "",
            },
        ]
        seen: list[str] = []
        spent = {"on": False}

        def fake_fetch(q, actress=None):
            seen.append(q)
            spent["on"] = True
            return [dict(row) for row in catalog]

        def mono():
            return 1000.0 if spent["on"] else 0.0

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_fetch), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(
            S, "enrich_title_candidate", side_effect=fake_enrich
        ), mock.patch("time.monotonic", side_effect=mono):
            rows = S._find_related_by_keywords(MIDA, max_n=5, budget_sec=6.0)

        self.assertIn("巨乳", seen, seen)
        self.assertNotEqual(seen[0], "巨乳")
        codes = [r["code"] for r in rows]
        for code in ("BODY-101", "BODY-202", "BODY-000"):
            self.assertIn(code, codes, codes)
        self.assertLess(codes.index("BODY-101"), codes.index("BODY-000"))
        self.assertLess(codes.index("BODY-202"), codes.index("BODY-000"))
        self.assertLess(codes.index("BODY-000"), codes.index("WEAK-001"))
        self.assertLess(codes.index("BODY-000"), codes.index("WEAK-002"))
        for row in rows:
            if row["code"].startswith("BODY-"):
                self.assertIn("巨乳", row.get("matched_keywords") or [])
            cover = str(row.get("cover") or "")
            self.assertFalse(cover.startswith("data:"))
            for u in row.get("stills") or []:
                self.assertFalse(str(u).startswith("data:"))

    def test_per_image_budget_and_worker_timeout_scale(self):
        self.assertEqual(S.SLOT_WORK_BUDGET_S, 600.0)
        self.assertEqual(S.GUNICORN_WORKER_TIMEOUT_S, 2400)
        self.assertFalse(hasattr(S, "MULTI_IDENTIFY_BUDGET_S"))
        root = os.path.join(ROOT)
        for name in ("Dockerfile", "Procfile", "railway.toml"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("--timeout 2400", text, name)
            self.assertNotIn("--timeout 600", text, name)
        S._notify_gunicorn_worker()
        kws = S._normalize_keyword_list(["ノーブラ誘惑", "巨乳"])
        self.assertEqual(kws[:3], ["ノーブラ", "誘惑", "巨乳"])
        qs = S._keyword_search_queries(MIDA, kws)
        self.assertIn("ノーブラ", qs)
        self.assertIn("誘惑", qs)
        self.assertNotIn("ノーブラ誘惑", qs)

    def test_identify_job_roundtrip_has_no_false_timeout(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            S._IDENTIFY_JOBS_PATH = Path(tmp) / "jobs.json"
            try:
                job_id = S.identify_job_create(4)
                self.assertTrue(job_id)
                S.identify_job_touch(job_id)
                S.identify_job_note(job_id, {"step": "vision", "status": "active", "phase": "辨識中"})
                payload = {"ok": True, "code": "AAA-001", "title": MIDA, "results": []}
                S.identify_job_finish(job_id, payload, 200)
                view = S.identify_job_public(job_id)
            finally:
                S._IDENTIFY_JOBS_PATH = None
        self.assertEqual(view["status"], "done")
        blob = json.dumps(view, ensure_ascii=False)
        for phrase in FALSE_TIMEOUT:
            self.assertNotIn(phrase, blob)
        self.assertEqual(view["result"]["code"], "AAA-001")


MISSAV_CN_HTML = """
<html><head>
<meta property="og:title" content="MIDA-616 女友妹妹的無胸罩誘惑 - MissAV">
</head><body>
<a href="https://missav.ai/cn/actresses/fukuda-yua">福田由愛</a>
</body></html>
"""

JABLE_HTML = """
<html><head>
<meta property="og:title" content="MIDA-100 巨乳泳社的集訓 - Jable.TV">
</head><body>
<a href="https://jable.tv/models/fukuda-yua/"><img alt="avatar"><span>福田由愛</span></a>
</body></html>
"""

MISSAV_JA_HTML = """
<html><head>
<meta property="og:title" content="MIDA-616 彼女の妹のノーブラ誘惑 - MissAV">
</head><body>
<a href="https://missav.ai/cn/actresses/fukuda-yua">福田ゆあ</a>
</body></html>
"""


class TestZhCatalogByCode(unittest.TestCase):
    """品番 pages on MissAV /cn/ and Jable fill 日文（中文）. No invented gloss."""

    def test_missav_cn_fills_title_and_actress(self):
        def fake_get(url, timeout=8.0, headers=None):
            if url == "https://missav.ai/cn/mida-616":
                return MISSAV_CN_HTML
            raise AssertionError(url)

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("MIDA-616", actress_ja="福田ゆあ", title_ja=MIDA)
        self.assertEqual(meta["title_zh"], "女友妹妹的無胸罩誘惑")
        self.assertEqual(meta["actress_zh"], "福田由愛")
        # The page title is used as-is. A keyword gloss is not substituted.
        self.assertNotEqual(meta["title_zh"], "無胸罩")

    def test_japanese_page_omits_parentheses_sources(self):
        def fake_get(url, timeout=8.0, headers=None):
            if "javlibrary" in url:
                return None
            if "mida-616" in url and "missav.ai/cn/" in url:
                return MISSAV_JA_HTML
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("mida-616", actress_ja="福田ゆあ")
        self.assertIsNone(meta["title_zh"])
        self.assertIsNone(meta["actress_zh"])

    def test_jable_when_missav_misses(self):
        seen = []

        def fake_get(url, timeout=8.0, headers=None):
            seen.append(url)
            if url == "https://jable.tv/videos/mida-100/":
                return JABLE_HTML
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("MIDA-100", actress_ja="福田ゆあ")
        self.assertEqual(meta["title_zh"], "巨乳泳社的集訓")
        self.assertEqual(meta["actress_zh"], "福田由愛")
        self.assertTrue(any("jable.tv" in u for u in seen))
        self.assertFalse(any("javlibrary" in u for u in seen))

    def test_same_han_name_is_not_a_second_billing(self):
        def fake_get(url, timeout=8.0, headers=None):
            if "missav.ai/cn/" in url:
                return MISSAV_CN_HTML
            return None

        with mock.patch.object(S, "http_get", side_effect=fake_get):
            meta = S.fetch_public_zh_catalog("MIDA-616", actress_ja="福田由愛")
        self.assertEqual(meta["title_zh"], "女友妹妹的無胸罩誘惑")
        self.assertIsNone(meta["actress_zh"])

    def test_attach_fills_main_and_related_without_inventing(self):
        def fake_get(url, timeout=8.0, headers=None):
            if url == "https://missav.ai/cn/mida-616":
                return MISSAV_CN_HTML
            if url == "https://jable.tv/videos/mida-100/":
                return JABLE_HTML
            if "missav.ai/cn/mida-100" in url:
                return None
            return None

        payload = {
            "code": "MIDA-616",
            "title": MIDA,
            "actress": "福田ゆあ",
            "related_by_title": [
                {
                    "code": "MIDA-100",
                    "title": "巨乳の合宿",
                    "actress": "福田ゆあ",
                }
            ],
        }
        with mock.patch.object(S, "http_get", side_effect=fake_get):
            out = S.attach_chinese_titles(payload, related_budget_sec=30)
        self.assertEqual(out["title_zh"], "女友妹妹的無胸罩誘惑")
        self.assertEqual(out["actress_zh"], "福田由愛")
        rel = out["related_by_title"][0]
        self.assertEqual(rel["title_zh"], "巨乳泳社的集訓")
        self.assertEqual(rel["actress_zh"], "福田由愛")

    def test_existing_chinese_title_is_not_replaced(self):
        def fake_get(url, timeout=8.0, headers=None):
            raise AssertionError(url)

        payload = {
            "code": "MIDA-616",
            "title": MIDA,
            "title_zh": "既有中文",
            "actress": "福田ゆあ",
            "related_by_title": [
                {"code": "MIDA-100", "title": "巨乳の合宿", "title_zh": "已有相關"}
            ],
        }
        with mock.patch.object(S, "http_get", side_effect=fake_get):
            out = S.attach_chinese_titles(payload, related_budget_sec=30)
        self.assertEqual(out["title_zh"], "既有中文")
        self.assertFalse(out.get("actress_zh"))
        self.assertEqual(out["related_by_title"][0]["title_zh"], "已有相關")
        self.assertFalse(out["related_by_title"][0].get("actress_zh"))

    def test_resolve_returns_title_and_reports_actress(self):
        def fake_get(url, timeout=8.0, headers=None):
            if "missav.ai/cn/mida-616" in url:
                return MISSAV_CN_HTML
            return None

        found: dict = {}
        with mock.patch.object(S, "http_get", side_effect=fake_get):
            zh = S.resolve_chinese_title(
                "MIDA-616",
                title_ja=MIDA,
                actress_ja="福田ゆあ",
                catalog_out=found,
            )
        self.assertEqual(zh, "女友妹妹的無胸罩誘惑")
        self.assertEqual(found.get("actress_zh"), "福田由愛")


if __name__ == "__main__":
    unittest.main()
