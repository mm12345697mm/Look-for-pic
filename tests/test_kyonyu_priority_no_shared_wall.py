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

        def fake_related(result, **kwargs):
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
        reject = getattr(S, "_title_has_ocr_garbage", None)
        if reject is not None:
            self.assertFalse(reject(SWIM))
            self.assertFalse(reject("巨乳水泳部員"))

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


if __name__ == "__main__":
    unittest.main()
