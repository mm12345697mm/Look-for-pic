#!/usr/bin/env python3
"""Round-trip several distinct works through title search, single identify, and multi.

No Gemini key and no live catalog. requests to avbase.net are answered with
__NEXT_DATA__ shaped like the real work pages; every other host returns 404.
Product ids and titles are fictional and do not overlap the demo package.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


def _work(code: str, title: str, actresses: list[str], studio: str) -> dict:
    cid = S.code_to_cid(code)
    return {
        "id": "fixture-" + code.lower().replace("-", ""),
        "work_id": code,
        "title": title,
        "actors": [{"name": name} for name in actresses],
        "products": [
            {
                "product_id": cid,
                "image_url": f"https://example.com/{cid}.jpg",
                "thumbnail_url": f"https://example.com/{cid}-thumb.jpg",
                "maker": {"name": studio},
                "actors": [{"name": name} for name in actresses],
            }
        ],
    }


# Distinctive titles: no shared 6-character window, and none score against the demo pack.
WORKS = [
    _work("KZTH-701", "星屑図書館で司書が朗読する午後の全記録", ["架空栞"], "架空書房"),
    _work("SWCD-318", "競泳表紙に番号だけが印刷された記録", ["架空泳"], "架空水泳"),
    # Fictional mechanic: this catalog title contains the OCR phrase. The real
    # 「舌技」 listing is APGH-012, and its catalog title does not contain 舌技.
    _work("SHJG-448", "舌技が神と呼ばれる架空の夜", ["架空ゆうき"], "架空夜"),
    _work("BODX-214", "深夜個室で繰り返された密会の全記録", ["架空密"], "架空密会"),
    _work("MACT-552", "双子姉妹が放課後に入れ替わる実験", ["架空綾", "架空凛"], "架空双子"),
    _work("LOCR-903", "温室の薔薇が散る前の約束", ["架空薔"], "架空温室"),
]
BY_CODE = {w["work_id"]: w for w in WORKS}


def _build_frames() -> list[dict]:
    specs = [
        {
            "style": "title-heavy",
            "code": "KZTH-701",
            "title": "星屑図書館で司書が朗読する午後の全記録",
            "actress": "架空栞",
            "not_actress": "別人栞",
            "vision": {"title": "星屑図書館で司書が朗読する", "actress": "別人栞"},
            "ocr": "",
        },
        {
            "style": "code-on-cover",
            "code": "SWCD-318",
            "title": "競泳表紙に番号だけが印刷された記録",
            "actress": "架空泳",
            "vision": {"code": "SWCD-318"},
            "ocr": "",
        },
        {
            "style": "short-distinctive",
            "code": "SHJG-448",
            "title": "舌技が神と呼ばれる架空の夜",
            "actress": "架空ゆうき",
            "not_actress": "柊ゆうき",
            "vision": {"title": "舌技が神", "actress": "柊ゆうき"},
            "ocr": "",
        },
        {
            "style": "bod-tagged",
            "code": "BODX-214",
            "title": "深夜個室で繰り返された密会の全記録",
            "actress": "架空密",
            "vision": {
                "title": "深夜個室で繰り返された密会の全記録 三比菜々美 (BOD)",
                "actress": "三比菜々美",
            },
            "ocr": "",
        },
        {
            "style": "multi-actress",
            "code": "MACT-552",
            "title": "双子姉妹が放課後に入れ替わる実験",
            "actress": "架空綾",
            "not_actress": "誤読双子",
            "vision": {"title": "双子姉妹が放課後に入れ替わる実験", "actress": "誤読双子"},
            "ocr": "",
        },
        {
            "style": "low-ocr",
            "code": "LOCR-903",
            "title": "温室の薔薇が散る前の約束",
            "actress": "架空薔",
            "vision": {},
            "ocr": "温室の薔薇\nxxx",
        },
        {
            "style": "still",
            "code": None,
            "title": None,
            "vision": {},
            "ocr": "",
        },
    ]
    frames = []
    for i, spec in enumerate(specs, start=1):
        row = dict(spec)
        row["index"] = i
        row["image"] = f"frame-{spec['style']}-{i}".encode()
        row["expect"] = spec["code"]
        frames.append(row)
    return frames


FRAMES = _build_frames()
VISION_BY_IMAGE = {row["image"]: row["vision"] for row in FRAMES}
OCR_BY_IMAGE = {row["image"]: row["ocr"] for row in FRAMES}
EXPECT_BY_IMAGE = {row["image"]: row["expect"] for row in FRAMES}


def _next_data(*, works=None, work=None) -> str:
    page: dict = {}
    if works is not None:
        page["works"] = works
    if work is not None:
        page["work"] = work
    blob = json.dumps({"props": {"pageProps": page}}, ensure_ascii=False)
    return f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>'


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = {}
        self.url = ""

    def json(self):
        return json.loads(self.text or "{}")


def _code_from_slug(slug: str) -> str:
    slug = unquote(slug or "").strip()
    if S.parse_code_parts(slug):
        return S.format_display_code(slug)
    matched = re.fullmatch(r"([A-Za-z]{2,10})(\d{1,5})", slug)
    if not matched:
        return ""
    return S.format_display_code(f"{matched.group(1)}-{matched.group(2)}")


def _matching_works(query: str) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    q_compact = re.sub(r"\s+", "", q)
    q_code = S.format_display_code(q) if S.parse_code_parts(q) else ""
    found: list[dict] = []
    for work in WORKS:
        title = str(work.get("title") or "")
        compact = re.sub(r"\s+", "", title)
        if q_code and S.codes_numeric_equal(q_code, str(work.get("work_id") or "")):
            found.append(work)
            continue
        if q in title or title in q or (q_compact and (q_compact in compact or compact in q_compact)):
            found.append(work)
    return found


def _fake_vision(image_bytes, mime, api_key):
    return dict(VISION_BY_IMAGE.get(image_bytes) or {})


def _fake_ocr(image_bytes):
    return OCR_BY_IMAGE.get(image_bytes, "")


def _fake_rank(user, cands, api_key=None, **kwargs):
    expect = EXPECT_BY_IMAGE.get(user)
    ranked = []
    for cand in cands or []:
        item = dict(cand)
        code = S.format_display_code(str(item.get("code") or ""))
        take = bool(expect) and code == expect
        item["visual"] = {
            "same_work": take,
            "confidence": 0.93 if take else 0.1,
            "match_person": take,
            "match_face": take,
            "match_accessories": take,
            "match_clothes": take,
            "match_pose": take,
        }
        item["visual_score"] = 0.93 if take else 0.1
        ranked.append(item)
    ranked.sort(key=lambda it: float(it.get("visual_score") or 0), reverse=True)
    best = (ranked[0].get("visual") if ranked else {}) or {}
    locked = bool(best.get("same_work") and best.get("match_clothes"))
    return ranked, {
        "visual_ranked": bool(ranked),
        "visual_lock": locked,
        "compared": len(ranked),
        "note": "locked" if locked else "open",
    }


class CatalogRoundTrip(unittest.TestCase):
    """One mocked catalog, three entry points: search, single image, multi upload."""

    @classmethod
    def setUpClass(cls):
        demo = S.load_demo().get("works") or {}
        for work in WORKS:
            title = work["title"]
            for raw in demo.values():
                if not isinstance(raw, dict):
                    continue
                score = S.title_similarity(title, str(raw.get("title") or ""))
                if score >= 0.55:
                    raise AssertionError(f"{work['work_id']} collides with demo ({score})")

    def setUp(self):
        self.urls: list[str] = []

        def fake_get(url, **kwargs):
            self.urls.append(str(url))
            parsed = urlparse(str(url))
            host = (parsed.hostname or "").lower()
            if "avbase.net" not in host:
                return _Resp("", 404)
            path = unquote(parsed.path or "")
            if path.rstrip("/").endswith("/works"):
                q = (parse_qs(parsed.query).get("q") or [""])[0]
                return _Resp(_next_data(works=_matching_works(q)))
            if "/works/" in path:
                code = _code_from_slug(path.rstrip("/").split("/")[-1])
                work = None
                for item in WORKS:
                    if code and S.codes_numeric_equal(code, item["work_id"]):
                        work = item
                        break
                if work is None:
                    return _Resp("", 404)
                return _Resp(_next_data(work=work))
            return _Resp("", 404)

        self._patches = [
            mock.patch.object(S.requests, "get", side_effect=fake_get),
            mock.patch.object(S.requests, "post", side_effect=AssertionError),
            mock.patch.object(S, "probe_cover_url", side_effect=lambda url, timeout=0: (True, url)),
            mock.patch.object(S, "offline_cache_get", return_value=None),
            mock.patch.object(S, "offline_cache_put", return_value=None),
            mock.patch.object(S, "resolve_chinese_title", return_value=None),
            mock.patch.object(S, "find_related_by_title", return_value=[]),
            mock.patch.object(S, "get_gemini_api_key", return_value="test-key"),
            mock.patch.object(S, "call_gemini_vision", side_effect=_fake_vision),
            mock.patch.object(S, "ocr_image_bytes", side_effect=_fake_ocr),
            mock.patch.object(S, "rank_candidates_by_visual", side_effect=_fake_rank),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for patch in self._patches:
            patch.stop()

    def test_edition_and_cast_suffix_keeps_catalog_title(self):
        cat = BY_CODE["BODX-214"]["title"]
        vision = cat + " 三比菜々美 (BOD)"
        self.assertEqual(S.choose_display_title(cat, vision), cat)
        self.assertEqual(S.choose_display_title(cat, cat + " VOL.2"), cat)
        kept = S.apply_vision_meta(
            {"title": cat, "title_zh": "既有中文", "actress": "架空密", "ok": True},
            {"title": vision, "actress": "三比菜々美"},
        )
        self.assertEqual(kept.get("title"), cat)
        self.assertNotIn("BOD", kept.get("title") or "")
        self.assertEqual(kept.get("title_zh"), "既有中文")
        self.assertEqual(kept.get("actress"), "架空密")

    def test_search_by_title_resolves_each_style(self):
        cases = [
            ("星屑図書館で司書が朗読する", None, "KZTH-701", "星屑図書館で司書が朗読する午後の全記録"),
            ("舌技が神", "柊ゆうき", "SHJG-448", "舌技が神と呼ばれる架空の夜"),
            ("舌技が神柊ゆうき", "柊ゆうき", "SHJG-448", "舌技が神と呼ばれる架空の夜"),
            (
                "深夜個室で繰り返された密会の全記録 三比菜々美 (BOD)",
                None,
                "BODX-214",
                "深夜個室で繰り返された密会の全記録",
            ),
            ("双子姉妹が放課後に入れ替わる実験", "誤読双子", "MACT-552", "双子姉妹が放課後に入れ替わる実験"),
            ("温室の薔薇", None, "LOCR-903", "温室の薔薇が散る前の約束"),
        ]
        for query, actress, code, title in cases:
            before = len(self.urls)
            hit = S.search_by_title(query, actress=actress)
            self.assertIsNotNone(hit, query)
            self.assertEqual(hit.get("code"), code, query)
            self.assertEqual(hit.get("title"), title, query)
            self.assertTrue(hit.get("cover"), query)
            touched = self.urls[before:]
            self.assertTrue(touched, query)
            self.assertTrue(all("avbase.net" in u for u in touched), touched)
            if actress:
                self.assertFalse(any(actress in unquote(u) for u in touched), (query, touched))

    def test_single_image_and_typed_query_resolve_code_and_catalog_title(self):
        for frame in FRAMES:
            payload, status = S.run_identify_pipeline(
                image_bytes=frame["image"],
                filename=frame["style"] + ".jpg",
            )
            if frame["style"] == "still":
                self.assertEqual(status, 200)
                self.assertFalse(payload.get("ok"))
                self.assertIn("未在圖片中找到番號或片名", payload.get("message") or "")
                continue
            self.assertEqual(status, 200, frame["style"])
            self.assertTrue(payload.get("ok"), (frame["style"], payload.get("message")))
            self.assertEqual(payload.get("code"), frame["code"], frame["style"])
            self.assertEqual(payload.get("title"), frame["title"], frame["style"])
            self.assertTrue(payload.get("cover"), frame["style"])
            self.assertNotIn("BOD", str(payload.get("title") or ""))
            if frame.get("actress"):
                self.assertEqual(payload.get("actress"), frame["actress"], frame["style"])
            if frame.get("not_actress"):
                self.assertNotIn(frame["not_actress"], str(payload.get("actress") or ""))

        typed, _status = S.run_identify_pipeline(image_bytes=None, user_title="舌技が神")
        self.assertEqual(typed.get("code"), "SHJG-448")
        self.assertEqual(typed.get("title"), "舌技が神と呼ばれる架空の夜")
        by_code, _status = S.run_identify_pipeline(image_bytes=None, user_code="SWCD-318")
        self.assertEqual(by_code.get("code"), "SWCD-318")
        self.assertEqual(by_code.get("title"), "競泳表紙に番号だけが印刷された記録")

    def test_multi_upload_keeps_one_slot_per_distinct_work(self):
        images = [(frame["image"], frame["style"] + ".jpg") for frame in FRAMES]
        notes = []

        def on_progress(event):
            if isinstance(event, dict):
                notes.append(str(event.get("detail") or ""))

        payload, status = S.run_multi_identify_pipeline(images, on_progress=on_progress)
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("image_count"), len(FRAMES))
        self.assertEqual(payload.get("dropped"), [])
        results = payload.get("results") or []
        self.assertEqual(len(results), len(FRAMES))
        blob = (payload.get("message") or "") + (payload.get("related_note") or "")
        self.assertNotIn("略過", blob)
        self.assertNotIn("去重", blob)
        self.assertTrue(any("待查 7 張" in note for note in notes), notes)
        self.assertTrue(any("搜尋第 7/7 張" in note for note in notes), notes)

        by_index = {row.get("from_image_index"): row for row in results}
        self.assertEqual(set(by_index), set(range(1, len(FRAMES) + 1)))
        for frame in FRAMES:
            row = by_index[frame["index"]]
            if frame["style"] == "still":
                self.assertTrue(row.get("unidentified"), row)
                self.assertEqual(row.get("code"), "TITLE-SEARCH")
                self.assertIn("尚未辨識", row.get("title") or "")
                continue
            self.assertEqual(row.get("code"), frame["code"], frame["style"])
            self.assertEqual(row.get("title"), frame["title"], frame["style"])
            self.assertTrue(row.get("cover"), frame["style"])
            self.assertFalse(row.get("needs_code"), frame["style"])
            self.assertNotIn("BOD", str(row.get("title") or ""))
            chips = " ".join(str(k) for k in (row.get("theme_keywords") or []))
            self.assertNotIn("BOD", chips.upper())
            if frame.get("actress"):
                self.assertEqual(row.get("actress"), frame["actress"], frame["style"])
            if frame.get("not_actress"):
                self.assertNotIn(frame["not_actress"], str(row.get("actress") or ""))


if __name__ == "__main__":
    unittest.main()
