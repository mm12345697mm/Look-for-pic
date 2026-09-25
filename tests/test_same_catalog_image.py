#!/usr/bin/env python3
"""Same-jacket visual pass, and related rescue when that pass does not lock."""
from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pattern(w: int, h: int, seed: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * seed + 20) % 255, (y * seed) % 255, ((x + y) * seed) % 255)
    return img


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


def _wide_with_front(front: Image.Image) -> Image.Image:
    w, h = front.size
    wide = Image.new("RGB", (w * 3, h), (18, 18, 22))
    wide.paste(front.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, 0))
    wide.paste(front, (w * 2, 0))
    return wide


class TestSameCatalogImageGate(unittest.TestCase):
    def test_scores_pass_same_picture_and_fail_a_different_one(self):
        front = _pattern(120, 160, 7)
        wide = _wide_with_front(front)
        frame, figure = S._same_catalog_image_scores(front, wide)
        self.assertTrue(S._is_same_catalog_image(frame, figure), (frame, figure))
        other = _pattern(120, 160, 3)
        wrong_f, wrong_g = S._same_catalog_image_scores(front, _wide_with_front(other))
        self.assertFalse(S._is_same_catalog_image(wrong_f, wrong_g), (wrong_f, wrong_g))
        poster = _smooth(180, 240, 9)
        crop = poster.crop((int(180 * 0.08), int(240 * 0.06), int(180 * 0.92), int(240 * 0.94)))
        crop_f, crop_g = S._same_catalog_image_scores(crop, poster)
        self.assertTrue(S._is_same_catalog_image(crop_f, crop_g), (crop_f, crop_g))
        stranger = _smooth(180, 240, 90)
        bad_f, bad_g = S._same_catalog_image_scores(stranger, poster)
        self.assertFalse(S._is_same_catalog_image(bad_f, bad_g), (bad_f, bad_g))

    def _payload(self, *, code="APGH-012", cover="https://cdn.example/apgh-012.jpg", stills=None, extra=None):
        row = {
            "code": code,
            "title": "先生が2人っきりのプライベート補習",
            "title_zh": "老師會在一對一私人補習中",
            "actress": "柊ゆうき",
            "cover": cover,
            "stills": list(stills or []),
        }
        payload = {
            "ok": True,
            "code": code,
            "title": row["title"],
            "title_zh": row["title_zh"],
            "actress": row["actress"],
            "cover": cover,
            "stills": list(stills or []),
            "candidates": [dict(row)],
        }
        if extra:
            payload["candidates"].extend(extra)
        return payload

    def _run_verify(self, payload, upload: bytes, blobs: dict, *, api_key="test-key", gemini=None):
        def fake_dl(url, timeout=None):
            return blobs.get(str(url))

        def keep_cover(code=None, cid=None, cover=None):
            return cid, cover, []

        if gemini is None:
            gemini = mock.Mock(side_effect=AssertionError("same jacket must not ask Gemini"))
        with mock.patch.object(S, "download_cover_bytes", side_effect=fake_dl), mock.patch.object(
            S, "sanitize_cover_fields", side_effect=keep_cover
        ), mock.patch.object(S, "gemini_rank_covers_batch", gemini), mock.patch.object(
            S, "get_gemini_api_key", return_value=""
        ):
            return S.verify_work_against_image(payload, upload, api_key=api_key)

    def test_identical_cover_clears_unverified_and_keeps_catalog_jacket(self):
        front = _png(_pattern(120, 160, 7))
        cover_url = "https://cdn.example/apgh-012.jpg"
        gemini = mock.Mock(side_effect=AssertionError("same jacket must not ask Gemini"))
        out = self._run_verify(self._payload(cover=cover_url), front, {cover_url: front}, gemini=gemini)
        self.assertEqual(out.get("code"), "APGH-012")
        self.assertTrue(out.get("visual_lock"))
        self.assertFalse(out.get("visual_mismatch"))
        self.assertNotIn("未核對", out.get("visual_note") or "")
        self.assertEqual(out.get("title_zh"), "老師會在一對一私人補習中")
        self.assertEqual(out.get("cover"), cover_url)
        self.assertFalse(str(out.get("cover") or "").startswith("data:"))
        gemini.assert_not_called()

    def test_front_of_wraparound_and_crop_pass_without_gemini(self):
        front_img = _pattern(100, 140, 7)
        wide = _png(_wide_with_front(front_img))
        cover_url = "https://cdn.example/wrap.jpg"
        out = self._run_verify(self._payload(cover=cover_url), _png(front_img), {cover_url: wide})
        self.assertTrue(out.get("visual_lock"), out.get("visual_meta"))
        self.assertFalse(out.get("visual_mismatch"))
        self.assertEqual((out.get("visual_meta") or {}).get("mode"), "same_catalog_image")
        self.assertEqual(out.get("cover"), cover_url)

        poster = _smooth(180, 240, 11)
        crop = poster.crop((int(180 * 0.08), int(240 * 0.06), int(180 * 0.92), int(240 * 0.94)))
        poster_url = "https://cdn.example/poster.jpg"
        cropped = self._run_verify(
            self._payload(cover=poster_url),
            _png(crop),
            {poster_url: _png(poster)},
        )
        self.assertTrue(cropped.get("visual_lock"), cropped.get("visual_note"))
        self.assertNotIn("未核對", cropped.get("visual_note") or "")
        self.assertEqual(cropped.get("title_zh"), "老師會在一對一私人補習中")

    def test_catalog_still_of_the_same_picture_locks_when_cover_differs(self):
        upload = _png(_pattern(120, 160, 7))
        other = _png(_pattern(120, 160, 4))
        cover_url = "https://cdn.example/cover.jpg"
        still_url = "https://cdn.example/still.jpg"
        out = self._run_verify(
            self._payload(cover=cover_url, stills=[still_url]),
            upload,
            {cover_url: other, still_url: upload},
        )
        self.assertTrue(out.get("visual_lock"))
        self.assertFalse(out.get("visual_mismatch"))
        self.assertEqual(out.get("cover"), cover_url)

    def test_different_outfit_stays_unverified(self):
        upload = _png(_pattern(120, 160, 7))
        cover = _png(_pattern(120, 160, 3))
        cover_url = "https://cdn.example/apgh-015.jpg"
        calls = {"n": 0}

        def gemini(user, covers, key, timeout=10.0, labels=None):
            calls["n"] += 1
            return [
                {
                    "same_work": False,
                    "confidence": 0.2,
                    "reason": "clothes differ",
                    "match_person": False,
                    "match_face": False,
                    "match_accessories": False,
                    "match_clothes": False,
                    "match_pose": False,
                }
                for _ in covers
            ]

        out = self._run_verify(
            self._payload(code="APGH-015", cover=cover_url),
            upload,
            {cover_url: cover},
            gemini=gemini,
        )
        self.assertGreaterEqual(calls["n"], 1)
        self.assertEqual(out.get("code"), "APGH-015")
        self.assertFalse(out.get("visual_lock"))
        self.assertTrue(out.get("visual_mismatch"))
        self.assertIn("未核對圖片", out.get("visual_note") or "")
        self.assertEqual(out.get("cover"), cover_url)

    def test_same_jacket_locks_with_no_vision_key(self):
        front = _png(_smooth(160, 200, 5))
        cover_url = "https://cdn.example/apgh-012.jpg"
        out = self._run_verify(
            self._payload(cover=cover_url),
            front,
            {cover_url: front},
            api_key="",
        )
        self.assertTrue(out.get("visual_lock"))
        self.assertNotIn("未核對", out.get("visual_note") or "")


class TestWeakVisualRelatedRescue(unittest.TestCase):
    def _candidate(self, code, title, *, actress="", cover=None, series=None):
        row = {"code": code, "title": title, "actress": actress, "cover": cover}
        if series:
            row["series"] = series
        return row

    def test_unverified_card_prefers_same_series_covers_over_drift(self):
        payload = {
            "ok": True,
            "code": "APGH-012",
            "title": "先生が2人っきりのプライベート補習で全部面倒みてあげる",
            "actress": "柊ゆうき",
            "visual_lock": False,
            "visual_mismatch": True,
            "visual_note": "未核對圖片（人物／衣服／姿勢與這張上傳圖不符）",
            "related_by_title": [
                {
                    "code": "ZZZZ-999",
                    "title": "全く別の海岸ドラマ",
                    "line": "theme",
                    "why": "片名相近",
                    "cover": "https://cdn.example/drift.jpg",
                }
            ],
            "candidates": [
                self._candidate(
                    "APGH-020",
                    "同じ系列の別巻でカバーなし",
                    actress="別人",
                ),
                self._candidate(
                    "APGH-015",
                    "同じ系列の隣巻",
                    actress="別人",
                    cover="https://cdn.example/apgh-015.jpg",
                ),
                self._candidate(
                    "OTHER-001",
                    "無関係な作品",
                    actress="別人",
                    cover="https://cdn.example/other.jpg",
                ),
                self._candidate(
                    "ABCD-003",
                    "別シリーズ",
                    actress="柊ゆうき",
                    cover="https://cdn.example/act.jpg",
                ),
            ],
        }
        out = S._rescue_weak_visual_related(payload)
        codes = [row.get("code") for row in out.get("related_by_title") or []]
        self.assertNotIn("ZZZZ-999", codes)
        self.assertNotIn("OTHER-001", codes)
        self.assertIn("APGH-015", codes)
        self.assertIn("APGH-020", codes)
        self.assertIn("ABCD-003", codes)
        self.assertLess(codes.index("APGH-015"), codes.index("APGH-020"))
        themed = [r for r in out["related_by_title"] if r.get("line") == "theme"]
        actress = [r for r in out["related_by_title"] if r.get("line") == "actress"]
        self.assertLessEqual(len(themed), 5)
        self.assertLessEqual(len(actress), 3)
        self.assertEqual(actress[0].get("code"), "ABCD-003")
        neighbor = next(r for r in out["related_by_title"] if r.get("code") == "APGH-015")
        self.assertEqual(neighbor.get("cover"), "https://cdn.example/apgh-015.jpg")
        self.assertFalse(neighbor.get("title_zh"))
        self.assertTrue(str(neighbor.get("cover")).startswith("https://"))

    def test_upload_url_never_becomes_the_related_jacket(self):
        payload = {
            "ok": True,
            "code": "APGH-012",
            "title": "プライベート補習",
            "visual_mismatch": True,
            "visual_lock": False,
            "related_by_title": [],
            "candidates": [
                {
                    "code": "APGH-015",
                    "title": "隣巻",
                    "cover": "data:image/jpeg;base64,aaaa",
                    "title_zh": "",
                }
            ],
        }
        out = S._rescue_weak_visual_related(payload)
        row = (out.get("related_by_title") or [None])[0]
        self.assertIsNotNone(row)
        self.assertEqual(row.get("code"), "APGH-015")
        self.assertFalse(str(row.get("cover") or "").startswith("data:"))
        self.assertFalse(row.get("title_zh"))

    def test_locked_card_does_not_drop_existing_related(self):
        payload = {
            "ok": True,
            "code": "APGH-012",
            "title": "プライベート補習",
            "visual_lock": True,
            "visual_mismatch": False,
            "related_by_title": [
                {"code": "ZZZZ-999", "title": "別", "line": "theme", "why": "片名相近"}
            ],
            "candidates": [
                {"code": "APGH-015", "title": "隣巻", "cover": "https://cdn.example/15.jpg"}
            ],
        }
        out = S._rescue_weak_visual_related(payload)
        codes = [row.get("code") for row in out.get("related_by_title") or []]
        self.assertEqual(codes, ["ZZZZ-999"])

    def test_does_not_blank_related_when_nothing_recoverable(self):
        payload = {
            "ok": True,
            "code": "APGH-012",
            "title": "短い題",
            "visual_mismatch": True,
            "related_by_title": [
                {"code": "ZZZZ-999", "title": "別", "line": "theme", "why": "片名相近"}
            ],
            "candidates": [],
        }
        out = S._rescue_weak_visual_related(payload)
        codes = [row.get("code") for row in out.get("related_by_title") or []]
        self.assertEqual(codes, ["ZZZZ-999"])

    def test_caps_stay_five_five_three(self):
        candidates = []
        for i in range(8):
            candidates.append(
                {
                    "code": f"APGH-{i+20:03d}",
                    "title": f"系列{i}",
                    "cover": f"https://cdn.example/{i}.jpg",
                }
            )
        for i in range(6):
            candidates.append(
                {
                    "code": f"ACT-{i+1:03d}",
                    "title": f"女優作{i}",
                    "actress": "柊ゆうき",
                    "cover": f"https://cdn.example/a{i}.jpg",
                }
            )
        payload = {
            "ok": True,
            "code": "APGH-012",
            "title": "別の短い題",
            "actress": "柊ゆうき",
            "visual_meta": {"visual_ranked": True, "visual_lock": False},
            "related_by_title": [],
            "candidates": candidates,
        }
        out = S._rescue_weak_visual_related(payload)
        rows = out.get("related_by_title") or []
        self.assertLessEqual(sum(1 for r in rows if r.get("line") == "theme"), 5)
        self.assertLessEqual(sum(1 for r in rows if r.get("line") == "keyword"), 5)
        self.assertLessEqual(sum(1 for r in rows if r.get("line") == "actress"), 3)
        self.assertGreaterEqual(sum(1 for r in rows if r.get("line") == "theme"), 1)
        self.assertEqual(sum(1 for r in rows if r.get("line") == "actress"), 3)

    def test_attach_uses_rescue_only_when_unlocked(self):
        found = [
            {
                "code": "ZZZZ-999",
                "title": "全く別の海岸ドラマ",
                "line": "theme",
                "why": "片名相近",
                "cover": "https://cdn.example/drift.jpg",
            }
        ]

        def run(lock):
            payload = {
                "ok": True,
                "code": "APGH-012",
                "title": "先生が2人っきりのプライベート補習で全部面倒みてあげる",
                "actress": "柊ゆうき",
                "visual_lock": lock,
                "visual_mismatch": not lock,
                "related_by_title": [],
                "candidates": [
                    {
                        "code": "APGH-015",
                        "title": "同じ系列の隣巻",
                        "cover": "https://cdn.example/15.jpg",
                    }
                ],
            }
            with mock.patch.object(S, "find_related_by_title", return_value=found), mock.patch.object(
                S, "attach_chinese_titles", side_effect=lambda payload, **k: payload
            ), mock.patch.object(
                S, "enrich_related_public_catalog", side_effect=lambda payload: payload
            ):
                return S.attach_related_by_title(payload, budget_sec=1, per_item=False)

        unlocked = run(False)
        unlocked_codes = [r.get("code") for r in unlocked.get("related_by_title") or []]
        self.assertIn("APGH-015", unlocked_codes)
        self.assertNotIn("ZZZZ-999", unlocked_codes)
        locked = run(True)
        locked_codes = [r.get("code") for r in locked.get("related_by_title") or []]
        self.assertEqual(locked_codes, ["ZZZZ-999"])
        self.assertTrue(str(locked["related_by_title"][0].get("cover")).startswith("https://"))


if __name__ == "__main__":
    unittest.main()
