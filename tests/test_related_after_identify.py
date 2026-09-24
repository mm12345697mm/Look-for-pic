"""A finished catalog hit must get related, even when the identify clock is spent.

#26 clipped multi-image related to the leftover of the identify deadline.
Once that leftover dropped under 1.5s, every slot — including the first
successful work — was stored with an empty carousel. These tests use
synthetic series codes only.
"""

from __future__ import annotations

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import server as S


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


TITLE = "架空系列の同じ題名で巻だけ違う"
ACTRESS = "架空花子"


def _catalog(code: str = "SER-012") -> dict:
    return {
        "ok": True,
        "code": code,
        "title": TITLE,
        "actress": ACTRESS,
        "studio": "架空",
        "cid": "ser00012",
        "cover": "https://example.com/ser012.jpg",
        "stills": ["https://example.com/ser012-1.jpg"],
        "related": [],
        "message": "來源：avbase",
    }


def _related_rows() -> list[dict]:
    return [
        {
            "code": "SER-011",
            "title": "架空系列の同じ題名で前の巻",
            "actress": ACTRESS,
            "line": "theme",
            "why": "片名相近",
            "cover": "https://example.com/ser011.jpg",
        },
        {
            "code": "SER-200",
            "title": "架空花子の別作品",
            "actress": ACTRESS,
            "line": "actress",
            "why": "同演員",
            "cover": "https://example.com/ser200.jpg",
        },
    ]


class TestRelatedAfterIdentifyClock(unittest.TestCase):
    def test_finished_slot_gets_related_when_identify_clock_is_spent(self):
        """The old skip was `_seconds_left(deadline) < 1.5` before any attach."""
        ok = _catalog()
        ok["related_by_title"] = []
        waiting = {
            "ok": True,
            "timed_out": True,
            "code": "TITLE-SEARCH",
            "title": "（這張尚未查完）",
            "related_by_title": [],
        }
        calls: list[str] = []

        def fake_attach(result, **kwargs):
            calls.append(str(result.get("code")))
            self.assertGreaterEqual(float(kwargs.get("budget_sec") or 0), S.MULTI_RELATED_SLOT_FLOOR_S)
            result["related_by_title"] = _related_rows()
            return result

        with mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach), mock.patch.object(
            S, "offline_cache_put"
        ):
            n = S._fill_related_for_finished_slots(
                [ok, waiting],
                identify_deadline=time.monotonic() - 2,
            )
        self.assertEqual(calls, ["SER-012"])
        self.assertGreaterEqual(n, 2)
        lines = {r.get("line") for r in ok["related_by_title"]}
        self.assertIn("theme", lines)
        self.assertIn("actress", lines)
        self.assertFalse(waiting.get("related_by_title"))

    def test_no_related_call_once_the_worker_wall_is_gone(self):
        ok = _catalog()
        with mock.patch.object(
            S, "attach_related_by_title", side_effect=AssertionError("related past the worker wall")
        ):
            n = S._fill_related_for_finished_slots(
                [ok],
                identify_deadline=time.monotonic() - 100,
            )
        self.assertEqual(n, 0)
        self.assertFalse(ok.get("related_by_title"))

    def test_two_finished_slots_both_run_when_the_window_is_open(self):
        rows = [_catalog("SER-012"), _catalog("SER-013")]
        for row in rows:
            row["related_by_title"] = []
        calls: list[str] = []

        def fake_attach(result, **kwargs):
            calls.append(str(result.get("code")))
            result["related_by_title"] = _related_rows()
            return result

        with mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach), mock.patch.object(
            S, "offline_cache_put"
        ):
            S._fill_related_for_finished_slots(
                rows,
                identify_deadline=time.monotonic() + 30,
            )
        self.assertEqual(calls, ["SER-012", "SER-013"])
        self.assertTrue(rows[0]["related_by_title"])
        self.assertTrue(rows[1]["related_by_title"])


class TestMultiPipelineRelated(unittest.TestCase):
    def _images(self, n: int) -> list[tuple[bytes, str]]:
        blob = _png()
        return [(blob, f"slot-{i + 1}.png") for i in range(n)]

    def test_short_deadline_still_identifies_every_slot_then_related(self):
        """Identify is not cut to save related time. Related still runs after."""
        calls: list[tuple[str, float]] = []

        def instant(**kwargs):
            row = _catalog(kwargs.get("user_code") or "SER-012")
            return row, 200

        def fake_attach(result, **kwargs):
            calls.append((str(result.get("code")), float(kwargs.get("budget_sec") or 0)))
            result["related_by_title"] = _related_rows()
            return result

        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", side_effect=["SER-012", "SER-013"]):
                with mock.patch.object(S, "run_identify_pipeline", side_effect=instant):
                    with mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach):
                        with mock.patch.object(S, "offline_cache_put"):
                            payload, status = S.run_multi_identify_pipeline(
                                self._images(2),
                                deadline=time.monotonic() - 1,
                            )
        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.get("timed_out") for r in results), payload.get("message"))
        self.assertEqual([c[0] for c in calls], ["SER-012", "SER-013"])
        self.assertTrue(all(c[1] >= S.MULTI_RELATED_SLOT_FLOOR_S for c in calls))
        for row in results:
            self.assertGreaterEqual(len(row.get("related_by_title") or []), 2)

    def test_open_deadline_fills_every_finished_slot(self):
        calls: list[str] = []

        def instant(**kwargs):
            return _catalog(kwargs.get("user_code") or "SER-012"), 200

        def fake_attach(result, **kwargs):
            code = str(result.get("code"))
            calls.append(code)
            result["related_by_title"] = [
                {
                    "code": "SER-099",
                    "title": TITLE,
                    "line": "theme",
                    "why": "片名相近",
                    "cover": "https://example.com/sib.jpg",
                }
            ]
            return result

        with mock.patch.object(S, "get_gemini_api_key", return_value=""):
            with mock.patch.object(S, "ocr_image_bytes", side_effect=["SER-012", "SER-013"]):
                with mock.patch.object(S, "run_identify_pipeline", side_effect=instant):
                    with mock.patch.object(S, "attach_related_by_title", side_effect=fake_attach):
                        with mock.patch.object(S, "offline_cache_put"):
                            payload, status = S.run_multi_identify_pipeline(
                                self._images(2),
                                deadline=time.monotonic() + 120,
                            )
        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.get("timed_out") for r in results))
        self.assertEqual(calls, ["SER-012", "SER-013"])
        for row in results:
            self.assertTrue(row.get("related_by_title"), row.get("code"))


class TestSingleAndCacheRelated(unittest.TestCase):
    def test_single_code_identify_attaches_related(self):
        def fake_find(title, exclude_code=None, **kwargs):
            self.assertIn("架空系列", str(title or ""))
            return _related_rows()

        with mock.patch.object(S, "offline_cache_get", return_value=None), mock.patch.object(
            S, "offline_cache_put"
        ), mock.patch.object(S, "identify_code", return_value=_catalog()), mock.patch.object(
            S, "find_related_by_title", side_effect=fake_find
        ), mock.patch.object(
            S, "attach_chinese_titles", side_effect=lambda payload, **kwargs: payload
        ):
            result, status = S.run_identify_pipeline(user_code="SER-012")
        self.assertEqual(status, 200)
        self.assertEqual(result.get("code"), "SER-012")
        rel = result.get("related_by_title") or []
        lines = {r.get("line") for r in rel}
        self.assertIn("theme", lines)
        self.assertIn("actress", lines)
        self.assertLessEqual(sum(1 for r in rel if r.get("line") == "theme"), 5)
        self.assertLessEqual(sum(1 for r in rel if r.get("line") == "actress"), 3)

    def test_cache_hit_with_empty_related_backfills(self):
        tmp = tempfile.TemporaryDirectory()
        prev = S._OFFLINE_CACHE_PATH
        S._OFFLINE_CACHE_PATH = Path(tmp.name) / "offline-cache.json"
        try:
            stored = _catalog()
            stored["related_by_title"] = []
            S.offline_cache_put(stored)

            def fake_find(title, exclude_code=None, **kwargs):
                return _related_rows()

            with mock.patch.object(S, "find_related_by_title", side_effect=fake_find), mock.patch.object(
                S, "resolve_chinese_title", return_value=None
            ), mock.patch.object(S, "fetch_avbase_by_code", return_value=None):
                result, status = S.run_identify_pipeline(user_code="SER-012")
            self.assertEqual(status, 200)
            self.assertTrue(result.get("from_offline_cache"))
            rel = result.get("related_by_title") or []
            codes = [r.get("code") for r in rel]
            self.assertIn("SER-011", codes)
            self.assertIn("SER-200", codes)
        finally:
            S._OFFLINE_CACHE_PATH = prev
            tmp.cleanup()


class TestKeywordCoverRank(unittest.TestCase):
    def test_keyword_bucket_prefers_real_cover_over_now_printing(self):
        """Titles that actually say 媚薬漬け. Not a stand-in for any one code."""
        title = "媚薬漬けで抗えない合宿"
        kws = S._extract_title_theme_keywords(title)
        self.assertIn("媚薬漬け", kws)
        self.assertNotIn("巨乳", kws)

        def fake_avbase(q, actress=None):
            return [
                {
                    "code": "DRUG-001",
                    "title": "媚薬漬けの空ジャケット",
                    "actress": "別人A",
                    "score": 0.99,
                    "cover": "https://pics.dmm.co.jp/digital/video/now_printing/now_printing.jpg",
                },
                {
                    "code": "DRUG-002",
                    "title": "媚薬漬けの本編",
                    "actress": "別人B",
                    "score": 0.40,
                    "cover": "https://pics.dmm.co.jp/digital/video/drug002/drug002pl.jpg",
                },
                {
                    "code": "DRUG-003",
                    "title": "媚薬漬けの無封面",
                    "actress": "別人C",
                    "score": 0.80,
                    "cover": "",
                },
            ]

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch.object(S, "fetch_avbase_title_results", side_effect=fake_avbase), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S._find_related_by_keywords(
                title,
                keywords=["媚薬漬け"],
                max_n=5,
                budget_sec=5.0,
            )
        codes = [r["code"] for r in rows]
        self.assertIn("DRUG-002", codes)
        self.assertEqual(codes[0], "DRUG-002", codes)
        if "DRUG-001" in codes:
            self.assertLess(codes.index("DRUG-002"), codes.index("DRUG-001"))


class TestFirstSlotKyonyuSurvivesBatch(unittest.TestCase):
    """#26's shared related clock emptied the first finished slot.

    A swim-camp title still has 巨乳 in its chips (#27). If related never
    runs, the keyword hits that used to sit under that chip disappear too.
    A title that does not say 巨乳 must not gain the chip.
    """

    SWIM = "巨乳水泳部員 媚薬漬けレ●プ合宿"
    DRUG = "媚薬漬けで抗えない合宿"

    def _slot(self, code: str, title: str) -> dict:
        return {
            "ok": True,
            "code": code,
            "title": title,
            "actress": "架空泳子",
            "cover": "https://example.com/camp.jpg",
            "related_by_title": [],
        }

    def _keyword_hits(self) -> list[dict]:
        return [
            {
                "code": "BODY-101",
                "title": "巨乳を媚薬漬けにした夜",
                "line": "keyword",
                "why": "關鍵字×2",
                "cover": "https://example.com/b1.jpg",
                "matched_keywords": ["巨乳", "媚薬漬け"],
                "keyword_hits": 2,
            },
            {
                "code": "BODY-202",
                "title": "巨乳だけの合宿",
                "line": "keyword",
                "why": "關鍵字×2",
                "cover": "https://example.com/b2.jpg",
                "matched_keywords": ["巨乳", "合宿"],
                "keyword_hits": 2,
            },
        ]

    def test_spent_identify_clock_still_keeps_kyonyu_on_the_first_slot(self):
        swim = self._slot("CAMP-100", self.SWIM)
        drug = self._slot("DRUG-100", self.DRUG)
        waiting = {
            "ok": True,
            "timed_out": True,
            "code": "尚未查完",
            "title": "（這張尚未查完）",
            "related_by_title": [],
        }
        seen_titles: list[str] = []

        def fake_find(title, **_kwargs):
            seen_titles.append(str(title or ""))
            if "巨乳" in str(title or ""):
                return self._keyword_hits()
            return [
                {
                    "code": "DRUG-002",
                    "title": "媚薬漬けの合宿記録",
                    "line": "keyword",
                    "why": "關鍵字×2",
                    "cover": "https://example.com/d2.jpg",
                    "matched_keywords": ["媚薬漬け", "合宿"],
                    "keyword_hits": 2,
                }
            ]

        with mock.patch.object(S, "find_related_by_title", side_effect=fake_find), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(S, "offline_cache_put"):
            n = S._fill_related_for_finished_slots(
                [swim, waiting, drug],
                identify_deadline=time.monotonic() - 2,
            )
        self.assertGreaterEqual(n, 2)
        self.assertTrue(any("巨乳" in t for t in seen_titles), seen_titles)
        self.assertIn("巨乳", swim.get("theme_keywords") or [], swim.get("theme_keywords"))
        self.assertIn("合宿", swim.get("theme_keywords") or [])
        keyword = [r for r in swim.get("related_by_title") or [] if r.get("line") == "keyword"]
        codes = [r.get("code") for r in keyword]
        self.assertIn("BODY-101", codes, codes)
        self.assertIn("BODY-202", codes, codes)
        self.assertTrue(all("巨乳" in str(r.get("title") or "") for r in keyword), keyword)
        self.assertFalse(waiting.get("related_by_title"))
        self.assertNotIn("巨乳", drug.get("theme_keywords") or [], drug.get("theme_keywords"))
        drug_kw = [r for r in drug.get("related_by_title") or [] if r.get("line") == "keyword"]
        self.assertTrue(drug_kw)
        self.assertTrue(all("巨乳" not in str(r.get("title") or "") for r in drug_kw))

    def test_multi_batch_first_result_keeps_kyonyu_keyword_hits(self):
        """The gallery's first card is results[0]. Its chips and keyword hits stay."""
        blob = _png()
        images = [(blob, "swim.png"), (blob, "other.png")]

        def instant(**kwargs):
            code = str(kwargs.get("user_code") or "")
            title = self.SWIM if code == "CAMP-100" else self.DRUG
            return self._slot(code or "CAMP-100", title), 200

        def fake_find(title, **_kwargs):
            if "巨乳" in str(title or ""):
                return self._keyword_hits()
            return [
                {
                    "code": "DRUG-002",
                    "title": "媚薬漬けの合宿記録",
                    "line": "keyword",
                    "why": "關鍵字×2",
                    "cover": "https://example.com/d2.jpg",
                    "matched_keywords": ["媚薬漬け", "合宿"],
                    "keyword_hits": 2,
                }
            ]

        with mock.patch.object(S, "get_gemini_api_key", return_value=""), mock.patch.object(
            S, "ocr_image_bytes", side_effect=["CAMP-100", "DRUG-100"]
        ), mock.patch.object(
            S, "run_identify_pipeline", side_effect=instant
        ), mock.patch.object(
            S, "verify_work_against_image", side_effect=lambda one, *a, **k: one
        ), mock.patch.object(
            S, "find_related_by_title", side_effect=fake_find
        ), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ), mock.patch.object(S, "offline_cache_put"):
            payload, status = S.run_multi_identify_pipeline(
                images,
                deadline=time.monotonic() + 120,
            )
        self.assertEqual(status, 200)
        results = payload.get("results") or []
        self.assertGreaterEqual(len(results), 1)
        first = results[0]
        self.assertEqual(first.get("code"), "CAMP-100")
        self.assertIn("巨乳", first.get("title") or "")
        self.assertIn("巨乳", first.get("theme_keywords") or [], first.get("theme_keywords"))
        self.assertIn("合宿", first.get("theme_keywords") or [])
        keyword = [r for r in first.get("related_by_title") or [] if r.get("line") == "keyword"]
        codes = [r.get("code") for r in keyword]
        self.assertIn("BODY-101", codes, codes)
        self.assertIn("BODY-202", codes, codes)
        self.assertEqual(payload.get("related_by_title"), first.get("related_by_title"))
        self.assertIn("巨乳", payload.get("theme_keywords") or [])
        if len(results) > 1 and results[1].get("code") == "DRUG-100":
            self.assertNotIn("巨乳", results[1].get("theme_keywords") or [])


class TestRelatedApiFreezesSavedMembership(unittest.TestCase):
    def test_under_cap_seed_is_not_researched_or_shrunk(self):
        seed = [
            {
                "code": f"THM-{i:03d}",
                "title": "テーマ作品タイトル",
                "title_zh": "中文",
                "line": "theme",
                "why": "片名相近",
            }
            for i in range(1, 10)
        ]
        with mock.patch.object(
            S, "find_related_by_title", side_effect=AssertionError("saved related must not be re-searched")
        ), mock.patch.object(S, "resolve_chinese_title", return_value=None):
            res = S.app.test_client().post(
                "/api/related-by-title",
                json={
                    "title": "巨乳水泳部員の合宿",
                    "code": "CAMP-100",
                    "actress": "誰か",
                    "seed": seed,
                },
            )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        codes = [r.get("code") for r in data.get("related_by_title") or []]
        self.assertEqual(codes, [f"THM-{i:03d}" for i in range(1, 10)])

    def test_upgrade_keeps_covered_seed_and_can_add(self):
        seed = [
            {
                "code": f"THM-{i:03d}",
                "title": "テーマ作品タイトル",
                "title_zh": "中文",
                "line": "theme",
                "why": "片名相近",
                "cover": f"https://example.com/t{i}.jpg",
            }
            for i in range(1, 10)
        ]
        extra = [
            {
                "code": "KEY-001",
                "title": "巨乳を媚薬漬けにした合宿",
                "line": "keyword",
                "why": "關鍵字×2",
                "cover": "https://example.com/k.jpg",
            }
        ]

        def fake_find(*_args, **_kwargs):
            return extra

        with mock.patch.object(S, "find_related_by_title", side_effect=fake_find), mock.patch.object(
            S, "resolve_chinese_title", return_value=None
        ):
            res = S.app.test_client().post(
                "/api/related-by-title",
                json={
                    "title": "巨乳水泳部員 媚薬漬け合宿",
                    "code": "CAMP-100",
                    "actress": "誰か",
                    "seed": seed,
                    "upgrade": True,
                },
            )
        self.assertEqual(res.status_code, 200)
        codes = [r.get("code") for r in (res.get_json() or {}).get("related_by_title") or []]
        self.assertEqual(codes[0], "KEY-001", codes)
        for i in range(1, 10):
            self.assertIn(f"THM-{i:03d}", codes)
        self.assertGreaterEqual(len(codes), 10)


class TestSwimCampBodyKeyword(unittest.TestCase):
    """Swim-camp shape: 巨乳 stays in the queries find_related actually runs.

    合宿 stays a strong chip. The keyword cap still prefers a mix of
    巨乳+媚薬漬け and 巨乳+合宿 over a bucket of 媚薬+合宿 with no 巨乳.
    """

    SWIM = "巨乳水泳部員 媚薬漬けレ●プ合宿"

    def test_find_related_queries_kyonyu_and_keeps_body_pairings(self):
        kws = S._extract_title_theme_keywords(self.SWIM)
        qs = S._keyword_search_queries(self.SWIM, kws)
        self.assertIn("巨乳", qs)
        self.assertIn("合宿", qs)
        self.assertFalse(S._is_weak_theme_token("合宿"))
        self.assertEqual(qs[:4], ["媚薬漬け", "巨乳", "水泳部員", "合宿"], qs)

        drug_rows = [
            {
                "code": f"DRUG-{i:03d}",
                "title": "媚薬漬けの合宿記録",
                "actress": "別人",
                "score": 0.99,
                "cover": "https://example.com/drug.jpg",
            }
            for i in range(1, 7)
        ]
        body_rows = [
            {
                "code": "BODY-101",
                "title": "巨乳を媚薬漬けにした夜",
                "actress": "別人",
                "score": 0.2,
                "cover": "https://example.com/body-drug.jpg",
            },
            {
                "code": "BODY-202",
                "title": "巨乳だけの合宿",
                "actress": "別人",
                "score": 0.2,
                "cover": "https://example.com/body-camp.jpg",
            },
        ]
        issued: list[str] = []
        clock = {"extra": 0.0}
        origin = __import__("time").monotonic()

        def mono():
            return origin + clock["extra"]

        def fake_avbase(q, actress=None):
            issued.append(q)
            if q == "媚薬漬け":
                clock["extra"] += 100.0
                return drug_rows
            if q == "巨乳":
                return body_rows
            return []

        def fake_enrich(c, why="片名候選"):
            item = dict(c)
            item["why"] = why
            item.setdefault("stills", [])
            return item

        with mock.patch("time.monotonic", side_effect=mono), mock.patch.object(
            S, "search_by_title", return_value=None
        ), mock.patch.object(
            S, "fetch_avbase_title_results", side_effect=fake_avbase
        ), mock.patch.object(
            S, "fetch_jav321_title_results", return_value=[]
        ), mock.patch.object(
            S, "fetch_javlibrary_title_results", return_value=[]
        ), mock.patch.object(
            S, "_find_related_by_actress", return_value=[]
        ), mock.patch.object(S, "enrich_title_candidate", side_effect=fake_enrich):
            rows = S.find_related_by_title(self.SWIM, exclude_code="CAMP-100", budget_sec=8.0)

        self.assertIn("巨乳", issued, issued)
        self.assertIn("合宿", issued, issued)
        keyword = [r for r in rows if r.get("line") == "keyword"]
        codes = [r.get("code") for r in keyword]
        self.assertLessEqual(len(keyword), 5, codes)
        self.assertIn("BODY-101", codes, codes)
        self.assertIn("BODY-202", codes, codes)
        self.assertTrue(
            any("巨乳" in str(r.get("title") or "") for r in keyword),
            codes,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
