#!/usr/bin/env python3
"""Accompanying Chinese glosses are shown and stored in Traditional script."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as S  # noqa: E402


class TestToTraditional(unittest.TestCase):
    def test_simplified_becomes_traditional(self):
        self.assertEqual(S.to_traditional_zh("我的老师在一对一私人补习中"), "我的老師在一對一私人補習中")
        self.assertEqual(S.to_traditional_zh("三上悠亚"), "三上悠亞")

    def test_traditional_and_empty_stay(self):
        self.assertEqual(S.to_traditional_zh("巨乳泳社"), "巨乳泳社")
        self.assertEqual(S.to_traditional_zh("老師會在一對一私人補習中"), "老師會在一對一私人補習中")
        self.assertIsNone(S.to_traditional_zh(None))
        self.assertEqual(S.to_traditional_zh(""), "")

    def test_japanese_text_is_not_rewritten(self):
        ja = "今日も息子の家庭教师とセックスしています。"
        self.assertEqual(S.to_traditional_zh(ja), ja)

    def test_without_converter_text_is_unchanged(self):
        with mock.patch.object(S, "_s2t_converter", return_value=None):
            self.assertEqual(S.to_traditional_zh("三上悠亚"), "三上悠亚")


class TestCleanersReturnTraditional(unittest.TestCase):
    def test_clean_title_zh(self):
        out = S._clean_title_zh("我的老师在一对一私人补习中", title_ja="個人授業", trusted=True)
        self.assertEqual(out, "我的老師在一對一私人補習中")

    def test_clean_actress_zh(self):
        self.assertEqual(S._clean_actress_zh("深田咏美", actress_ja="深田えいみ"), "深田詠美")


class TestPayloadGlosses(unittest.TestCase):
    def test_identify_exit_converts_main_results_and_related(self):
        payload = {
            "ok": True,
            "code": "ABC-001",
            "title": "日本語のタイトル",
            "title_zh": "补习老师",
            "actress": "三上悠亜",
            "actress_zh": "三上悠亚",
            "cover": "https://pics.dmm.co.jp/digital/video/abc00001/abc00001pl.jpg",
            "related_by_title": [{"code": "ABC-002", "title": "別", "title_zh": "头发", "line": "theme"}],
            "results": [
                {
                    "code": "ABC-001",
                    "title_zh": "补习老师",
                    "studio_zh": "发行商",
                    "related_by_title": [{"code": "ABC-003", "title_zh": "里面", "line": "keyword"}],
                }
            ],
        }
        S._lock_identify_payload_media(payload)
        self.assertEqual(payload["title"], "日本語のタイトル")
        self.assertEqual(payload["actress"], "三上悠亜")
        self.assertEqual(payload["title_zh"], "補習老師")
        self.assertEqual(payload["actress_zh"], "三上悠亞")
        self.assertEqual(payload["related_by_title"][0]["title_zh"], "頭髮")
        self.assertEqual(payload["results"][0]["title_zh"], "補習老師")
        self.assertEqual(payload["results"][0]["studio_zh"], "發行商")
        self.assertEqual(payload["results"][0]["related_by_title"][0]["title_zh"], "裡面")

    def test_missing_gloss_is_not_invented(self):
        payload = {"ok": True, "code": "ABC-001", "title": "日本語", "related_by_title": [{"code": "ABC-002"}]}
        S._lock_identify_payload_media(payload)
        self.assertNotIn("title_zh", payload)
        self.assertNotIn("title_zh", payload["related_by_title"][0])


if __name__ == "__main__":
    unittest.main()
