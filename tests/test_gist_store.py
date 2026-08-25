"""gist_store の単体テスト。ネットワークには接続せず urlopen を差し替える。"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gist_store


class FakeResponse:
    """urllib.request.urlopen の戻り値を模したオブジェクト。"""

    def __init__(self, obj):
        self._body = json.dumps(obj).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LoadStateTest(unittest.TestCase):

    def test_ファイルが無ければ空の状態を返す(self):
        with patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse({"files": {}})):
            state = gist_store.load_state("gid", "tok")
        self.assertEqual(state, {"offset": 0, "messages": []})

    def test_中身が空文字なら空の状態を返す(self):
        payload = {"files": {"memoryex-state.json": {"content": "  "}}}
        with patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(payload)):
            state = gist_store.load_state("gid", "tok")
        self.assertEqual(state, {"offset": 0, "messages": []})

    def test_保存済みの内容を読み出す(self):
        stored = {"offset": 42, "messages": [{"update_id": 41, "text": "メモ"}]}
        payload = {"files": {"memoryex-state.json": {"content": json.dumps(stored)}}}
        with patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(payload)):
            state = gist_store.load_state("gid", "tok")
        self.assertEqual(state["offset"], 42)
        self.assertEqual(state["messages"][0]["text"], "メモ")

    def test_キーが欠けていても補完する(self):
        payload = {"files": {"memoryex-state.json": {"content": "{}"}}}
        with patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(payload)):
            state = gist_store.load_state("gid", "tok")
        self.assertEqual(state, {"offset": 0, "messages": []})

    def test_切り詰められていれば例外にする(self):
        payload = {"files": {"memoryex-state.json": {"content": "{}", "truncated": True}}}
        with patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(payload)):
            with self.assertRaises(RuntimeError):
                gist_store.load_state("gid", "tok")

    def test_空の状態は毎回別のリストを返す(self):
        a = gist_store.empty_state()
        b = gist_store.empty_state()
        a["messages"].append("x")
        self.assertEqual(b["messages"], [])


class SaveStateTest(unittest.TestCase):

    def test_PATCHで内容を送る(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"id": "gid"})

        with patch("urllib.request.urlopen", fake_urlopen):
            gist_store.save_state("gid", "tok", {"offset": 5, "messages": []})

        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["url"], "https://api.github.com/gists/gid")
        self.assertEqual(captured["auth"], "Bearer tok")
        content = captured["body"]["files"]["memoryex-state.json"]["content"]
        self.assertEqual(json.loads(content)["offset"], 5)

    def test_日本語をエスケープせずに保存する(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"id": "gid"})

        with patch("urllib.request.urlopen", fake_urlopen):
            gist_store.save_state("gid", "tok", {"offset": 1, "messages": [{"text": "散歩"}]})

        content = captured["body"]["files"]["memoryex-state.json"]["content"]
        self.assertIn("散歩", content)


if __name__ == "__main__":
    unittest.main()
