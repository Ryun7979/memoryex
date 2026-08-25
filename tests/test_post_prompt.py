"""format_with_gemini() が振る ID と coverage.build_labeled_items() のキーの
整合性を検証する回帰テスト。

Task 7 のレビューで指摘された「両者がずれてもテストで検知できない」問題への
対応として追加した。ここが壊れると、Gemini が返す coverage の抜粋と本文照合の
対応が取れなくなり、常に全項目が「未反映」と判定されて記事1本あたり最大3回の
無駄な Gemini 再生成が走ってしまう。
"""

import datetime as dt
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# post.py はモジュール読み込み時に複数の環境変数を要求するため、import 前に
# ダミー値を設定する（実際の API キーは不要）。
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("JUGEM_USER", "dummy")
os.environ.setdefault("JUGEM_PASS", "dummy")

import coverage
import post


class _FixedDate:
    """Windows には strftime の %-m / %-d が無いため、Linux 互換の挙動をする
    最小限の日付ラッパー。post.py 自体は変更しない。"""

    def __init__(self, real: dt.date):
        self._real = real

    def weekday(self):
        return self._real.weekday()

    def strftime(self, fmt):
        fmt = fmt.replace("%-m", str(self._real.month)).replace("%-d", str(self._real.day))
        return self._real.strftime(fmt)


def _fake_call_gemini(prompt, effort="low"):
    """Gemini を呼ばず、JSON パースが通る最小限のダミー応答を返す。"""
    return '{"title": "t", "body": "<p>b</p>", "coverage": {}}'


class FormatWithGeminiIdConsistencyTest(unittest.TestCase):
    """format_with_gemini() が組み立てるプロンプト中の ID と、
    coverage.build_labeled_items() が返すキー集合が一致することを検証する。"""

    MESSAGES = ["散歩した", "本を読んだ"]
    GCAL_EVENTS = ["10:00 会議", "15:00 歯医者"]
    GITHUB_ACTIVITY = ["memoryex に 3 コミット"]
    URL_SUMMARIES = [
        {"title": "面白い記事", "description": "AIについて", "url": "https://example.com/a"},
    ]
    LOCATION_NAMES = ["京都駅"]
    MOVIE_INFOS = [
        {"title": "映画A", "runtime": "120分", "genres": "ドラマ", "overview": "概要"},
    ]
    # 歩数・睡眠・移動距離・消費カロリー・アクティブ時間・運動の6項目全てを与え、
    # H の採番順が2箇所（プロンプト側と coverage 側）で一致することを確実に検証する。
    HEALTH_DATA = {
        "steps": 8000,
        "sleep_minutes": 420,
        "distance_km": 5.2,
        "calories": 2000,
        "active_minutes": 30,
        "exercises": ["ランニング"],
    }

    def _capture_prompt(self) -> str:
        """format_with_gemini() を呼び、実際に組み立てられたプロンプト文字列を返す。
        _call_gemini はテスト内でのみ差し替え、終了時に必ず元へ戻す。"""
        captured = {}

        def fake_call_gemini(prompt, effort="low"):
            captured["prompt"] = prompt
            return _fake_call_gemini(prompt, effort)

        with patch.object(post, "_call_gemini", fake_call_gemini):
            post.format_with_gemini(
                self.MESSAGES,
                weather="晴れ",
                github_activity=self.GITHUB_ACTIVITY,
                gcal_events=self.GCAL_EVENTS,
                url_summaries=self.URL_SUMMARIES,
                location_names=self.LOCATION_NAMES,
                movie_infos=self.MOVIE_INFOS,
                news_headlines=[],
                health_data=self.HEALTH_DATA,
                target_date=_FixedDate(dt.date(2026, 8, 25)),
            )
        self.assertIn("prompt", captured, "_call_gemini が呼ばれていない")
        return captured["prompt"]

    def test_プロンプト中のID集合とbuild_labeled_itemsのキー集合が一致する(self):
        prompt = self._capture_prompt()
        found_ids = set(re.findall(r"\[([MCGULVH]\d+)\]", prompt))
        expected_ids = set(coverage.build_labeled_items(
            self.MESSAGES, self.GCAL_EVENTS, self.GITHUB_ACTIVITY,
            self.URL_SUMMARIES, self.LOCATION_NAMES, self.MOVIE_INFOS,
            self.HEALTH_DATA,
        ).keys())
        self.assertEqual(found_ids, expected_ids)

    # 健康データの各項目を一意に識別できるキーワード。
    # post.py 側の行文言（例:「・歩数: ...」）と coverage._health_labels() 側の
    # 行文言（例:「健康: 歩数 ...」）の双方に共通して含まれる語を選んでいる。
    HEALTH_KEYWORDS = ["歩数", "睡眠時間", "移動距離", "消費カロリー", "アクティブ時間", "運動"]

    @staticmethod
    def _find_id_for_keyword(text: str, keyword: str) -> str:
        """text の中でキーワードを含む行から先頭の [X1] 形式の ID を取り出す。
        見つからなければ None を返す。"""
        for line in text.splitlines():
            if keyword in line:
                m = re.search(r"\[([MCGULVH]\d+)\]", line)
                if m:
                    return m.group(1)
        return None

    def test_健康データは項目ごとにプロンプトとcoverage側でIDが一致する(self):
        # ID の「集合」が一致していても、健康データは post.py 側で行を手組みして
        # いるため、coverage._health_labels() と行の並び順がずれると
        # 同じ項目に別々の ID が振られてしまう可能性がある（集合比較だけでは
        # 検出できない）。項目ごとに ID を突き合わせて、この種のずれを検出する。
        prompt = self._capture_prompt()
        expected_items = coverage.build_labeled_items(
            [], [], [], [], [], [], self.HEALTH_DATA,
        )
        # キーワード -> coverage 側で振られた ID
        expected_id_by_keyword = {}
        for key, label in expected_items.items():
            for keyword in self.HEALTH_KEYWORDS:
                if keyword in label:
                    expected_id_by_keyword[keyword] = key
                    break

        self.assertEqual(
            sorted(expected_id_by_keyword.keys()), sorted(self.HEALTH_KEYWORDS),
            "テストデータの前提が崩れている（coverage側で全キーワードが見つからない）",
        )

        for keyword in self.HEALTH_KEYWORDS:
            prompt_id = self._find_id_for_keyword(prompt, keyword)
            expected_id = expected_id_by_keyword[keyword]
            self.assertEqual(
                prompt_id, expected_id,
                f"「{keyword}」の ID がプロンプト側（{prompt_id}）と "
                f"coverage.build_labeled_items() 側（{expected_id}）でずれている",
            )

    def test_健康データのH採番が複数項目でも両者で一致する(self):
        prompt = self._capture_prompt()
        found_h_ids = sorted(re.findall(r"\[(H\d+)\]", prompt))
        expected_h_ids = sorted(
            key for key in coverage.build_labeled_items(
                [], [], [], [], [], [], self.HEALTH_DATA,
            ).keys()
            if key.startswith("H")
        )
        # 6項目全て（歩数・睡眠・移動距離・消費カロリー・アクティブ時間・運動）が
        # 採番されていることも合わせて確認する。
        self.assertEqual(len(found_h_ids), 6)
        self.assertEqual(found_h_ids, expected_h_ids)


if __name__ == "__main__":
    unittest.main()
