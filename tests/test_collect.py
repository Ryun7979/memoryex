"""collect の単体テスト。ネットワークには接続しない。"""

import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect

JST = timezone(timedelta(hours=9))
CHAT_ID = "12345"


def ts_of(y, m, d, hh=12, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=JST).timestamp())


def update(update_id, text="メモ", chat_id=CHAT_ID, message_id=1,
           ts=None, key="message", caption=None, location=None):
    msg = {"message_id": message_id, "chat": {"id": int(chat_id)},
           "date": ts if ts is not None else ts_of(2026, 8, 25)}
    if text is not None:
        msg["text"] = text
    if caption is not None:
        msg["caption"] = caption
    if location is not None:
        msg["location"] = location
    return {"update_id": update_id, key: msg}


class ExtractMessagesTest(unittest.TestCase):

    def test_対象チャットのテキストを取り出す(self):
        got = collect.extract_messages([update(10, "散歩した", message_id=7)], CHAT_ID)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "散歩した")
        self.assertEqual(got[0]["update_id"], 10)
        self.assertEqual(got[0]["message_id"], 7)
        self.assertEqual(got[0]["date"], "2026-08-25")

    def test_対象外のチャットは除外する(self):
        got = collect.extract_messages([update(10, "他人の話", chat_id="999")], CHAT_ID)
        self.assertEqual(got, [])

    def test_写真のキャプションもメモとして扱う(self):
        got = collect.extract_messages([update(10, text=None, caption="ラーメン")], CHAT_ID)
        self.assertEqual(got[0]["text"], "ラーメン")

    def test_編集後のメッセージも拾う(self):
        got = collect.extract_messages(
            [update(11, "直したメモ", key="edited_message")], CHAT_ID)
        self.assertEqual(got[0]["text"], "直したメモ")

    def test_チャンネル投稿も拾う(self):
        got = collect.extract_messages([update(12, "投稿", key="channel_post")], CHAT_ID)
        self.assertEqual(got[0]["text"], "投稿")

    def test_位置情報を保存する(self):
        got = collect.extract_messages(
            [update(13, text=None, location={"latitude": 35.0, "longitude": 135.7})], CHAT_ID)
        self.assertEqual(got[0]["location"], {"lat": 35.0, "lon": 135.7})

    def test_本文も位置情報も無ければ捨てる(self):
        got = collect.extract_messages([update(14, text=None)], CHAT_ID)
        self.assertEqual(got, [])

    def test_日付はJSTで判定する(self):
        # 2026-08-25 00:30 JST は UTC では 08-24。JST 基準で 08-25 になること
        got = collect.extract_messages(
            [update(15, "深夜メモ", ts=ts_of(2026, 8, 25, 0, 30))], CHAT_ID)
        self.assertEqual(got[0]["date"], "2026-08-25")


class NextOffsetTest(unittest.TestCase):

    def test_最大のupdate_idに1を足す(self):
        ups = [update(10), update(12), update(11)]
        self.assertEqual(collect.next_offset(ups, 5), 13)

    def test_更新が無ければ現状を維持する(self):
        self.assertEqual(collect.next_offset([], 7), 7)

    def test_対象外チャットの更新も確定させる(self):
        ups = [update(20, chat_id="999")]
        self.assertEqual(collect.next_offset(ups, 5), 21)


class MergeMessagesTest(unittest.TestCase):

    def _item(self, update_id, message_id, text, d="2026-08-25", ts=None):
        return {"update_id": update_id, "message_id": message_id, "text": text,
                "date": d, "ts": ts if ts is not None else ts_of(2026, 8, 25)}

    def test_新旧を結合する(self):
        existing = [self._item(1, 1, "古いメモ", ts=ts_of(2026, 8, 25, 8))]
        new = [self._item(2, 2, "新しいメモ", ts=ts_of(2026, 8, 25, 20))]
        got = collect.merge_messages(existing, new, date(2026, 8, 25))
        self.assertEqual([m["text"] for m in got], ["古いメモ", "新しいメモ"])

    def test_同じmessage_idは編集後で置き換える(self):
        existing = [self._item(1, 5, "編集前")]
        new = [self._item(9, 5, "編集後")]
        got = collect.merge_messages(existing, new, date(2026, 8, 25))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "編集後")

    def test_保持日数より古いものを捨てる(self):
        existing = [
            self._item(1, 1, "4日前", d="2026-08-21", ts=ts_of(2026, 8, 21)),
            self._item(2, 2, "2日前", d="2026-08-23", ts=ts_of(2026, 8, 23)),
        ]
        got = collect.merge_messages(existing, [], date(2026, 8, 25))
        self.assertEqual([m["text"] for m in got], ["2日前"])

    def test_時刻の昇順で並べる(self):
        existing = [self._item(2, 2, "夜", ts=ts_of(2026, 8, 25, 21))]
        new = [self._item(3, 3, "朝", ts=ts_of(2026, 8, 25, 7))]
        got = collect.merge_messages(existing, new, date(2026, 8, 25))
        self.assertEqual([m["text"] for m in got], ["朝", "夜"])


class FakeResponse:
    def __init__(self, obj):
        self._body = json.dumps(obj).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FetchUpdatesTest(unittest.TestCase):

    def test_long_pollingで待つ(self):
        # timeout=0（即時リターン）だと、キューが用意される前に空で返ることがある。
        # 投稿直前の収集で空振りしないよう long polling で待つことを保証する。
        captured = {}

        def fake_urlopen(url, timeout=0):
            captured["url"] = url
            captured["http_timeout"] = timeout
            return FakeResponse({"ok": True, "result": []})

        with patch("urllib.request.urlopen", fake_urlopen):
            collect.fetch_updates("TOKEN", 0)

        self.assertGreater(collect.LONG_POLL_SECONDS, 0)
        self.assertIn(f"timeout={collect.LONG_POLL_SECONDS}", captured["url"])
        # HTTP 側のタイムアウトが long polling の待ち時間より短いと必ず失敗する
        self.assertGreater(captured["http_timeout"], collect.LONG_POLL_SECONDS)

    def test_offsetが0なら省略する(self):
        captured = {}

        def fake_urlopen(url, timeout=0):
            captured["url"] = url
            return FakeResponse({"ok": True, "result": []})

        with patch("urllib.request.urlopen", fake_urlopen):
            collect.fetch_updates("TOKEN", 0)

        self.assertNotIn("offset=", captured["url"])
        self.assertIn("edited_message", captured["url"])

    def test_offsetが正なら付与する(self):
        captured = {}

        def fake_urlopen(url, timeout=0):
            captured["url"] = url
            return FakeResponse({"ok": True, "result": [{"update_id": 1}]})

        with patch("urllib.request.urlopen", fake_urlopen):
            got = collect.fetch_updates("TOKEN", 99)

        self.assertIn("offset=99", captured["url"])
        self.assertEqual(got, [{"update_id": 1}])

    def test_okがfalseなら例外にする(self):
        def fake_urlopen(url, timeout=0):
            return FakeResponse({"ok": False, "description": "Unauthorized"})

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError):
                collect.fetch_updates("TOKEN", 0)


class CollectOnceTest(unittest.TestCase):

    def test_dry_runではGistに書き込まない(self):
        saved = []
        state = {"offset": 5, "messages": []}
        ups = [update(10, "新しいメモ", message_id=3)]

        with patch.object(collect.gist_store, "load_state", lambda *a, **k: state), \
             patch.object(collect.gist_store, "save_state", lambda *a, **k: saved.append(a)), \
             patch.object(collect, "fetch_updates", lambda *a, **k: ups):
            got = collect.collect_once("T", CHAT_ID, "gid", "tok",
                                       today=date(2026, 8, 25), dry_run=True)

        self.assertEqual(saved, [])
        self.assertEqual(got["offset"], 11)
        self.assertEqual(got["messages"][0]["text"], "新しいメモ")

    def test_通常実行ではGistに保存する(self):
        saved = []
        state = {"offset": 5, "messages": []}
        ups = [update(10, "新しいメモ", message_id=3)]

        with patch.object(collect.gist_store, "load_state", lambda *a, **k: state), \
             patch.object(collect.gist_store, "save_state",
                          lambda gid, tok, st, **k: saved.append(st)), \
             patch.object(collect, "fetch_updates", lambda *a, **k: ups):
            collect.collect_once("T", CHAT_ID, "gid", "tok", today=date(2026, 8, 25))

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["offset"], 11)

    def test_Gist保存失敗時は例外を握りつぶさず送出する(self):
        state = {"offset": 5, "messages": []}
        ups = [update(10, "新しいメモ", message_id=3)]

        def raise_error(*a, **k):
            raise RuntimeError("Gist への保存に失敗")

        with patch.object(collect.gist_store, "load_state", lambda *a, **k: state), \
             patch.object(collect.gist_store, "save_state", raise_error), \
             patch.object(collect, "fetch_updates", lambda *a, **k: ups):
            with self.assertRaises(RuntimeError):
                collect.collect_once("T", CHAT_ID, "gid", "tok", today=date(2026, 8, 25))


if __name__ == "__main__":
    unittest.main()
