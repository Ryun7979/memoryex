# Telegram メモ収集の分離と反映漏れの機械的検証 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram メモを 2 時間ごとに Secret Gist へ収集して取りこぼしをゼロにし、記事への反映漏れ検証を LLM 採点から機械的照合に置き換える。

**Architecture:** 収集（`collect.py`）と記事化（`post.py`）を分離する。`collect.py` は `getUpdates` の `offset` を確定させながら Secret Gist（`gist_store.py`）に追記し、Telegram の 24 時間保持制限から解放する。`post.py` は Gist から読み出し、生成結果は `coverage.py` の純粋関数で本文と機械照合する。

**Tech Stack:** Python 3.12（GitHub Actions）/ Python 3.14（ローカル）、標準ライブラリのみ、テストは `unittest`、CI は GitHub Actions。

## Global Constraints

- **外部ライブラリを追加しない。** 既存 `post.py` と同じく標準ライブラリのみで実装する（`requirements.txt` は作らない）。
- **Gemini API の呼び出し回数を増やさない。** `collect.py` は Gemini を一切呼ばない。
- **メモ本文をリポジトリにコミットしない。** リポジトリは public。メモは Secret Gist にのみ置く。
- **ローカルでの Python 実行は `py` コマンドを使う。** Git Bash の `python` は WindowsApps のスタブで動作しない。GitHub Actions 上は `python` を使う。
- **コミットはユーザーが手動で行う。** 各タスクの Commit ステップはコミットメッセージ案の提示までとし、実行者は `git add` / `git commit` を実行しない。ブランチも作らず `main` で作業する。
- Gist のファイル名は `memoryex-state.json` 固定。
- Secrets 名は `GIST_ID`（32 桁の gist ID）と `GIST_TOKEN`（classic PAT・`gist` スコープ）。登録済み。
- 日付は全て JST（UTC+9）基準。
- コメント・ログ出力・docstring は日本語で書く（既存 `post.py` の流儀に合わせる）。

---

### Task 1: gist_store.py — Secret Gist の読み書き

**Files:**
- Create: `gist_store.py`
- Test: `tests/test_gist_store.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `gist_store.empty_state() -> dict` … `{"offset": 0, "messages": []}`
  - `gist_store.normalize_state(raw: dict) -> dict`
  - `gist_store.load_state(gist_id: str, token: str, filename: str = "memoryex-state.json") -> dict`
  - `gist_store.save_state(gist_id: str, token: str, state: dict, filename: str = "memoryex-state.json") -> None`
  - `gist_store.DEFAULT_FILENAME: str`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_gist_store.py` を新規作成する。

```python
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
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'gist_store'`）

- [ ] **Step 3: gist_store.py を実装する**

```python
"""Secret Gist を JSON 状態ファイルの保存先として読み書きする。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import json
import urllib.request

GIST_API = "https://api.github.com/gists"
DEFAULT_FILENAME = "memoryex-state.json"


def empty_state() -> dict:
    """初期状態を返す。呼び出しごとに新しいリストを作る。"""
    return {"offset": 0, "messages": []}


def normalize_state(raw) -> dict:
    """欠けたキーを補い、想定した型に揃える。"""
    if not isinstance(raw, dict):
        return empty_state()
    offset = raw.get("offset")
    messages = raw.get("messages")
    return {
        "offset": offset if isinstance(offset, int) and offset > 0 else 0,
        "messages": messages if isinstance(messages, list) else [],
    }


def _request(url: str, token: str, method: str = "GET", payload: bytes = None) -> dict:
    """GitHub Gist API を呼び出して JSON を返す。"""
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "memoryex",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


def load_state(gist_id: str, token: str, filename: str = DEFAULT_FILENAME) -> dict:
    """Gist から状態を読み出す。ファイルが無い・空の場合は初期状態を返す。"""
    data = _request(f"{GIST_API}/{gist_id}", token)
    entry = (data.get("files") or {}).get(filename)
    if not entry:
        return empty_state()
    if entry.get("truncated"):
        raise RuntimeError(
            f"Gist の {filename} が大きすぎて切り詰められました。保持件数を減らしてください"
        )
    content = (entry.get("content") or "").strip()
    if not content:
        return empty_state()
    try:
        return normalize_state(json.loads(content))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gist の {filename} が JSON として不正です: {e}")


def save_state(gist_id: str, token: str, state: dict, filename: str = DEFAULT_FILENAME) -> None:
    """Gist に状態を書き戻す。"""
    body = json.dumps(
        {"files": {filename: {"content": json.dumps(state, ensure_ascii=False, indent=1)}}}
    ).encode()
    _request(f"{GIST_API}/{gist_id}", token, method="PATCH", payload=body)
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: PASS（8 tests）

- [ ] **Step 5: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
feat: Secret Gist を状態保存先として読み書きする gist_store を追加
```

対象ファイル: `gist_store.py`, `tests/test_gist_store.py`

---

### Task 2: collect.py — 収集の純粋関数

**Files:**
- Create: `collect.py`（純粋関数部分のみ。I/O は Task 3 で追加）
- Test: `tests/test_collect.py`

**Interfaces:**
- Consumes: なし（Task 1 とは独立）
- Produces:
  - `collect.JST: timezone`
  - `collect.ALLOWED_UPDATES: list[str]`
  - `collect.extract_messages(updates: list, chat_id: str) -> list[dict]`
    - 返す辞書: `{"update_id": int, "message_id": int, "ts": int, "date": "YYYY-MM-DD", "text": str}`。位置情報があれば `"location": {"lat": float, "lon": float}` を追加
  - `collect.next_offset(updates: list, current: int) -> int`
  - `collect.merge_messages(existing: list, new: list, today: datetime.date, keep_days: int = 3) -> list[dict]`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_collect.py` を新規作成する。

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'collect'`）

- [ ] **Step 3: collect.py の純粋関数を実装する**

```python
"""Telegram のメモを収集して Secret Gist に保存するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 収集対象の更新種別。編集されたメモも拾えるよう edited_* を含める
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post"]


def _pick_message(update: dict) -> dict:
    """更新から本体のメッセージを取り出す。"""
    for key in ALLOWED_UPDATES:
        msg = update.get(key)
        if msg:
            return msg
    return {}


def extract_messages(updates: list, chat_id: str) -> list:
    """対象チャットの更新をメモ用の辞書に正規化する。

    本文（text / caption）も位置情報も無いメッセージは保存しない。
    """
    out = []
    for update in updates:
        msg = _pick_message(update)
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        ts = int(msg.get("date", 0))
        item = {
            "update_id": int(update.get("update_id", 0)),
            "message_id": int(msg.get("message_id", 0)),
            "ts": ts,
            "date": datetime.fromtimestamp(ts, tz=JST).date().isoformat(),
            "text": (msg.get("text") or msg.get("caption") or "").strip(),
        }
        loc = msg.get("location")
        if loc:
            item["location"] = {"lat": loc["latitude"], "lon": loc["longitude"]}
        if not item["text"] and "location" not in item:
            continue
        out.append(item)
    return out


def next_offset(updates: list, current: int) -> int:
    """次回 getUpdates に渡す offset を求める。

    対象外チャットの更新も確定させないとキューに残り続けるため、
    抽出結果ではなく取得した全更新から算出する。
    """
    ids = [int(u["update_id"]) for u in updates if "update_id" in u]
    return max(ids) + 1 if ids else current


def merge_messages(existing: list, new: list, today, keep_days: int = 3) -> list:
    """既存と新規をマージする。

    同じ message_id は update_id が大きい方（編集後）を残し、
    保持日数より古い日付のメモは捨てる。
    """
    merged = {}
    for item in list(existing) + list(new):
        key = item.get("message_id") or item.get("update_id")
        prev = merged.get(key)
        if prev is None or item.get("update_id", 0) >= prev.get("update_id", 0):
            merged[key] = item
    cutoff = (today - timedelta(days=keep_days - 1)).isoformat()
    kept = [m for m in merged.values() if m.get("date", "") >= cutoff]
    return sorted(kept, key=lambda m: (m.get("ts", 0), m.get("update_id", 0)))
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: PASS（合計 23 tests）

- [ ] **Step 5: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
feat: メモ収集の正規化・マージ・offset 算出を collect に追加
```

対象ファイル: `collect.py`, `tests/test_collect.py`

---

### Task 3: collect.py — Telegram 取得と CLI

**Files:**
- Create: `notify.py`
- Modify: `collect.py`（先頭の import を更新し、`merge_messages` の後ろに追記）
- Test: `tests/test_collect.py`（末尾にテストクラスを追加）

**Interfaces:**
- Consumes:
  - `gist_store.load_state(gist_id, token)` / `gist_store.save_state(gist_id, token, state)`（Task 1）
  - `collect.extract_messages` / `collect.next_offset` / `collect.merge_messages`（Task 2）
- Produces:
  - `notify.send_error(token: str, chat_id: str, text: str) -> None`
  - `collect.fetch_updates(token: str, offset: int, limit: int = 100) -> list`
  - `collect.collect_once(telegram_token, chat_id, gist_id, gist_token, today=None, dry_run=False) -> dict`
    - 戻り値は保存後の state（`{"offset": int, "messages": list}`）
  - `collect.main() -> None`（CLI。`--dry-run` を受け付ける）

- [ ] **Step 1: notify.py を作る**

`post.py` の `notify_telegram_error()` は `JUGEM_USER` などモジュール読み込み時に必須の環境変数を持つため、`collect.py` から import できない。通知処理だけを独立モジュールに切り出す。

```python
"""Telegram へ通知を送る最小限のヘルパ。外部ライブラリ不要。"""

import json
import urllib.request


def send_error(token: str, chat_id: str, text: str) -> None:
    """エラー通知を送る。通知自体の失敗は握りつぶす（本処理を止めない）。"""
    payload = json.dumps({
        "chat_id": chat_id,
        "text": f"⚠️ memoryex 失敗\n\n{text[:3000]}",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            json.loads(res.read())
    except Exception as e:
        print(f"  [WARN] Telegram 通知失敗: {e}")
```

- [ ] **Step 2: fetch_updates と collect_once のテストを追記する**

`tests/test_collect.py` の `MergeMessagesTest` の後ろ、`if __name__ == "__main__":` の前に以下を追加する。

```python
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
```

- [ ] **Step 3: テストを実行して失敗を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: FAIL（`AttributeError: module 'collect' has no attribute 'fetch_updates'`、または `... has no attribute 'gist_store'`。どちらも未実装を示す）

- [ ] **Step 4: collect.py に I/O と CLI を実装する**

ファイル冒頭の import 群を以下に差し替える。

```python
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import gist_store
import notify
```

`merge_messages` の後ろに以下を追記する。

```python
def fetch_updates(token: str, offset: int, limit: int = 100) -> list:
    """Telegram から未確定の更新を取得する。

    offset を渡した時点でそれより前の更新は確定（confirm）され、
    サーバー側のキューから外れる。保存済みの範囲のみを確定させること。
    """
    params = {
        "limit": str(limit),
        "timeout": "0",
        "allowed_updates": json.dumps(ALLOWED_UPDATES),
    }
    if offset > 0:
        params["offset"] = str(offset)
    url = (f"https://api.telegram.org/bot{token}/getUpdates?"
           + urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=30) as res:
        data = json.loads(res.read())
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API エラー: {data}")
    return data.get("result", [])


def collect_once(telegram_token: str, chat_id: str, gist_id: str, gist_token: str,
                 today=None, dry_run: bool = False) -> dict:
    """1 回分の収集を行い、保存後の state を返す。

    Gist への保存が成功した場合にのみ offset が永続化されるため、
    途中で失敗しても次回実行で同じ範囲を取り直せる。
    """
    if today is None:
        today = datetime.now(JST).date()

    state = gist_store.load_state(gist_id, gist_token)
    updates = fetch_updates(telegram_token, state["offset"])
    new_items = extract_messages(updates, chat_id)
    next_state = {
        "offset": next_offset(updates, state["offset"]),
        "messages": merge_messages(state["messages"], new_items, today),
    }

    print(f"  取得した更新: {len(updates)} 件 / 新規メモ: {len(new_items)} 件")
    print(f"  保存後のメモ総数: {len(next_state['messages'])} 件")
    print(f"  次回 offset: {state['offset']} -> {next_state['offset']}")
    for item in new_items:
        preview = item["text"][:40] or "(位置情報)"
        print(f"    + [{item['date']}] {preview}")

    if dry_run:
        print("  [dry-run] Gist へは書き込みません")
        return next_state

    gist_store.save_state(gist_id, gist_token, next_state)
    return next_state


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    telegram_token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gist_id = os.environ["GIST_ID"]
    gist_token = os.environ["GIST_TOKEN"]

    print(f"=== 収集開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')} ===", flush=True)
    try:
        collect_once(telegram_token, chat_id, gist_id, gist_token, dry_run=dry_run)
    except Exception as e:
        print(f"❌ 収集失敗: {e}")
        notify.send_error(telegram_token, chat_id, f"メモ収集に失敗しました: {e}")
        raise
    print("✅ 収集完了")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: テストを実行して成功を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: PASS（合計 28 tests）

- [ ] **Step 6: 実際の Gist に対して dry-run で疎通確認する**

このコマンドはユーザー自身に実行してもらう（トークンをシェル履歴に残さないため）。

```bash
TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... GIST_ID=... GIST_TOKEN=... py collect.py --dry-run
```

Expected: 「取得した更新: N 件」と「次回 offset: 0 -> M」が表示され、`[dry-run] Gist へは書き込みません` で終わる。401/404 が出た場合はローカルに渡した値を確認する。

- [ ] **Step 7: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
feat: Telegram 取得と Gist 保存を行う collect の CLI を追加
```

対象ファイル: `collect.py`, `notify.py`, `tests/test_collect.py`

---

### Task 4: GitHub Actions ワークフロー

**Files:**
- Create: `.github/workflows/collect.yml`
- Modify: `.github/workflows/daily_post.yml`（env に `GIST_ID` / `GIST_TOKEN` を追加）

**Interfaces:**
- Consumes: `collect.py`（Task 3）
- Produces: 2 時間ごとに Gist を更新する定期実行

- [ ] **Step 1: collect.yml を作る**

```yaml
name: Collect Telegram Memos

on:
  schedule:
    - cron: "0 */2 * * *"    # 2時間ごと（Telegram の24時間保持制限に対する余裕を確保）
  workflow_dispatch:
    inputs:
      dry_run:
        description: "DRY_RUN: Gist に書き込まず取得内容だけ表示"
        required: false
        default: false
        type: boolean

# 前の実行が終わる前に次が始まると offset が競合するため直列化する
concurrency:
  group: collect-telegram-memos
  cancel-in-progress: false

jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: リポジトリをチェックアウト
        uses: actions/checkout@v7

      - name: Python をセットアップ
        uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: メモ収集スクリプトを実行
        env:
          TELEGRAM_TOKEN:   ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GIST_ID:          ${{ secrets.GIST_ID }}
          GIST_TOKEN:       ${{ secrets.GIST_TOKEN }}
          PYTHONUNBUFFERED: "1"
        run: python collect.py ${{ inputs.dry_run && '--dry-run' || '' }}
```

- [ ] **Step 2: daily_post.yml に Secrets を追加する**

`.github/workflows/daily_post.yml` の `env:` ブロック、`TMDB_API_KEY` の行の直後に以下 2 行を追加する。

```yaml
          GIST_ID:          ${{ secrets.GIST_ID }}
          GIST_TOKEN:       ${{ secrets.GIST_TOKEN }}
```

- [ ] **Step 3: dry-run で手動実行して確認する**

1. 変更を push する（ユーザーが手動）。**`schedule` トリガーは既定ブランチに存在しないと有効にならないため、main への push が必要**
2. GitHub の Actions タブ → 「Collect Telegram Memos」→ Run workflow → `dry_run` を **true** にして実行
3. ログに「取得した更新: N 件」が出ること、エラーが無いことを確認する

Expected: 成功（緑）。401 なら `GIST_TOKEN`、404 なら `GIST_ID` を疑う。

- [ ] **Step 4: 本番実行して Gist が更新されることを確認する**

1. 同じ画面で `dry_run` を **false**（既定）のまま Run workflow
2. Gist のページを再読み込みし、`memoryex-state.json` の中身が `{}` から `{"offset": ..., "messages": [...]}` に変わっていることを確認する

Expected: `offset` が 0 以外になり、当日送ったメモが `messages` に入っている。

- [ ] **Step 5: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
feat: メモ収集を2時間ごとに実行するワークフローを追加
```

対象ファイル: `.github/workflows/collect.yml`, `.github/workflows/daily_post.yml`

---

### Task 5: post.py を Gist 読み出しへ切り替える

**Files:**
- Modify: `post.py`（import 追加、設定追加、`get_today_messages()` の置き換え、`notify_telegram_error()` の共通化）

**Interfaces:**
- Consumes: `collect.collect_once` / `collect.fetch_updates` / `collect.extract_messages`（Task 2・3）、`notify.send_error`（Task 3）
- Produces: `post.get_today_messages()` … 戻り値は現行と同じ `(messages, url_summaries, location_names, movie_infos, target_date)`

- [ ] **Step 1: import と設定を追加する**

`post.py` の import 群（`from datetime import ...` の直後）に追加する。

```python
import collect
import notify
```

`TMDB_API_KEY` の行の直後に追加する。

```python
GIST_ID               = os.environ.get("GIST_ID", "")
GIST_TOKEN            = os.environ.get("GIST_TOKEN", "")
```

- [ ] **Step 2: get_today_messages() を置き換える**

`post.py:703` から始まる `get_today_messages()` のうち、**Telegram を直接叩く部分（`url = (` の組み立てから診断ログ出力まで）を削除**し、以下に差し替える。URL 要約・逆ジオコーディング・映画情報を取得する後半（`# URL のタイトル・概要を取得（最大5件）` 以降）と `return` はそのまま残す。

```python
def _load_collected_messages(target_date) -> list[dict]:
    """収集を1回実行し、Gist から保存済みメモを読む。

    Gist が使えない場合は Telegram を直接読む。その際は offset を渡さないため
    サーバー側の更新は確定されず、次回の収集で取り直せる。
    """
    if GIST_ID and GIST_TOKEN:
        try:
            state = collect.collect_once(
                TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GIST_ID, GIST_TOKEN,
                today=target_date,
            )
            return state["messages"]
        except Exception as e:
            print(f"  [WARN] Gist の読み書きに失敗: {e}")
            print("  [WARN] Telegram を直接読み出します（収集は次回に持ち越し）")
    else:
        print("  [WARN] GIST_ID / GIST_TOKEN が未設定のため Telegram を直接読み出します")

    updates = collect.fetch_updates(TELEGRAM_TOKEN, 0)
    return collect.extract_messages(updates, TELEGRAM_CHAT_ID)


def get_today_messages():
    """収集済みメモから対象日のメモ・URL・位置情報・映画を取り出す。"""
    now_jst = datetime.now(JST)
    # 深夜0〜6時に実行された場合は前日のメッセージを対象にする
    if now_jst.hour < 6:
        target_date = (now_jst - timedelta(days=1)).date()
        print(f"  （深夜実行のため前日 {target_date} のメッセージを取得）")
    else:
        target_date = now_jst.date()

    # 取得ウィンドウの下限は「対象日の前日 23:00」。
    # 投稿は 23:00 起動のため、対象日ちょうどで区切ると前日 23:00〜24:00 に
    # 送ったメモがどの実行でも拾われず永久に消える。
    cutoff = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=JST
    ) - timedelta(hours=1)
    cutoff_ts = int(cutoff.timestamp())

    stored = _load_collected_messages(target_date)
    target = [m for m in stored if m.get("ts", 0) >= cutoff_ts]

    messages: list[str] = []
    url_pattern = re.compile(r"https?://\S+")
    movie_pattern = re.compile(r"^映画[　 :：、,]?\s*(.+)", re.MULTILINE)
    found_urls: list[str] = []
    found_locations: list[dict] = []
    found_movies: list[str] = []

    for item in target:
        text = item.get("text", "")
        if text:
            messages.append(text)
            for u in url_pattern.findall(text):
                if u not in found_urls:
                    found_urls.append(u)
            for m in movie_pattern.findall(text):
                t = m.strip()
                if t and t not in found_movies:
                    found_movies.append(t)
        loc = item.get("location")
        if loc:
            found_locations.append(loc)

    # 診断ログ: 保存済みメモが何件あり、そのうち何件が対象日かを毎回出力する
    diag_dates: dict[str, int] = {}
    for m in stored:
        d = m.get("date", "不明")
        diag_dates[d] = diag_dates.get(d, 0) + 1
    date_summary = ", ".join(f"{d}: {n}件" for d, n in sorted(diag_dates.items())) or "なし"
    print(f"  [診断] 保存済みメモ: {len(stored)} 件 / 対象日: {target_date}")
    print(f"  [診断] 取得範囲: {cutoff.strftime('%m/%d %H:%M')} 以降 → {len(target)} 件")
    print(f"  [診断] 日付別の内訳: {date_summary}")
```

（この直後に、現行の `# URL のタイトル・概要を取得（最大5件）` 以降のコードがそのまま続く）

なお、逆ジオコーディングのループは `found_locations` の要素が `{"lat": ..., "lon": ...}` である前提のままで動く（`collect.extract_messages` が同じキーで保存するため）。

- [ ] **Step 3: notify_telegram_error を共通実装に寄せる**

`post.py:1178` の `notify_telegram_error()` の本体を以下に置き換える（関数名と呼び出し側は変更しない）。

```python
def notify_telegram_error(message: str) -> None:
    """エラー発生時に Telegram へ失敗通知を送る。"""
    notify.send_error(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
```

- [ ] **Step 4: 構文とテストを確認する**

Run: `py -c "import ast,io; ast.parse(io.open('post.py',encoding='utf-8').read()); print('構文OK')"`
Expected: `構文OK`

Run: `py -m unittest discover -s tests -v`
Expected: PASS（合計 28 tests。既存テストが壊れていないことの確認）

- [ ] **Step 5: 手動実行で動作を確認する**

GitHub の Actions タブ → 「Daily Blog Post」→ Run workflow（`test_mode` は false）で実行する。

Expected: ログに `[診断] 保存済みメモ: N 件` が出て、当日のメモ件数が期待どおりであること。記事が投稿され Telegram に通知が届くこと。

- [ ] **Step 6: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
refactor: post.py のメモ取得を Gist 経由に切り替え
```

対象ファイル: `post.py`

---

### Task 6: coverage.py — 反映漏れの機械照合

**Files:**
- Create: `coverage.py`
- Test: `tests/test_coverage.py`

**Interfaces:**
- Consumes: なし（純粋関数のみ）
- Produces:
  - `coverage.MIN_EXCERPT_LEN: int`（= 8）
  - `coverage.strip_html(body: str) -> str`
  - `coverage.build_labeled_items(messages, gcal_events, github_activity, url_summaries, location_names, movie_infos, health_data) -> dict[str, str]`
    - 例: `{"M1": "メモ: 散歩した", "C1": "予定: 10:00 会議", "H1": "健康: 歩数 8,000歩"}`
  - `coverage.find_missing(items: dict, body: str, coverage_map) -> list[str]`（未反映項目のラベル一覧）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_coverage.py` を新規作成する。

```python
"""coverage の純粋関数の単体テスト。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coverage


class BuildLabeledItemsTest(unittest.TestCase):

    def test_種別ごとに連番のIDを振る(self):
        items = coverage.build_labeled_items(
            messages=["散歩した", "本を読んだ"],
            gcal_events=["10:00 会議"],
            github_activity=["memoryex に 3 コミット"],
            url_summaries=[{"title": "記事タイトル", "url": "https://example.com"}],
            location_names=["京都駅"],
            movie_infos=[{"title": "映画A"}],
            health_data={"steps": 8000},
        )
        self.assertEqual(items["M1"], "メモ: 散歩した")
        self.assertEqual(items["M2"], "メモ: 本を読んだ")
        self.assertEqual(items["C1"], "予定: 10:00 会議")
        self.assertEqual(items["G1"], "GitHub: memoryex に 3 コミット")
        self.assertEqual(items["U1"], "共有URL: 記事タイトル")
        self.assertEqual(items["L1"], "訪問場所: 京都駅")
        self.assertEqual(items["V1"], "映画: 映画A")
        self.assertEqual(items["H1"], "健康: 歩数 8,000歩")

    def test_URLはタイトルが無ければURLを使う(self):
        items = coverage.build_labeled_items(
            [], [], [], [{"title": "", "url": "https://example.com"}], [], [], {})
        self.assertEqual(items["U1"], "共有URL: https://example.com")

    def test_健康データは存在する項目だけ並べる(self):
        items = coverage.build_labeled_items(
            [], [], [], [], [], [],
            {"steps": 8000, "sleep_minutes": 425, "distance_km": 5.2})
        self.assertEqual(items["H1"], "健康: 歩数 8,000歩")
        self.assertEqual(items["H2"], "健康: 睡眠時間 7時間5分")
        self.assertEqual(items["H3"], "健康: 移動距離 5.2km")
        self.assertNotIn("H4", items)

    def test_睡眠が丁度の時間なら分を省く(self):
        items = coverage.build_labeled_items([], [], [], [], [], [], {"sleep_minutes": 420})
        self.assertEqual(items["H1"], "健康: 睡眠時間 7時間")

    def test_データが無ければ空になる(self):
        self.assertEqual(coverage.build_labeled_items([], [], [], [], [], [], {}), {})


class FindMissingTest(unittest.TestCase):

    BODY = "<p>今日は鴨川沿いを一時間ほど散歩した。</p><p>夜は本を読んで過ごした。</p>"

    def test_本文に実在する抜粋なら反映済みとみなす(self):
        items = {"M1": "メモ: 散歩した"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを一時間ほど散歩した"})
        self.assertEqual(got, [])

    def test_タグをまたぐ抜粋も照合できる(self):
        items = {"M1": "メモ: 読書"}
        got = coverage.find_missing(items, self.BODY, {"M1": "夜は本を読んで過ごした"})
        self.assertEqual(got, [])

    def test_空白の違いを無視する(self):
        items = {"M1": "メモ: 散歩"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを 一時間ほど 散歩した"})
        self.assertEqual(got, [])

    def test_本文に無い抜粋は未反映とする(self):
        items = {"M1": "メモ: 歯医者に行った"}
        got = coverage.find_missing(items, self.BODY, {"M1": "歯医者で治療を受けた"})
        self.assertEqual(got, ["メモ: 歯医者に行った"])

    def test_キーが無ければ未反映とする(self):
        items = {"M1": "メモ: 散歩", "M2": "メモ: 歯医者"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを一時間ほど散歩した"})
        self.assertEqual(got, ["メモ: 歯医者"])

    def test_短すぎる抜粋は未反映とする(self):
        items = {"M1": "メモ: 散歩"}
        got = coverage.find_missing(items, self.BODY, {"M1": "散歩"})
        self.assertEqual(got, ["メモ: 散歩"])

    def test_coverageが辞書でなければ全て未反映とする(self):
        items = {"M1": "メモ: 散歩", "M2": "メモ: 読書"}
        got = coverage.find_missing(items, self.BODY, None)
        self.assertEqual(got, ["メモ: 散歩", "メモ: 読書"])

    def test_項目が無ければ空を返す(self):
        self.assertEqual(coverage.find_missing({}, self.BODY, {}), [])


class StripHtmlTest(unittest.TestCase):

    def test_タグと空白を除去する(self):
        self.assertEqual(coverage.strip_html("<p>あ い</p>\n<p>う</p>"), "あいう")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'coverage'`）

- [ ] **Step 3: coverage.py を実装する**

```python
"""記事本文にデータ項目が反映されているかを機械的に照合する。

Gemini には「各項目に対応する本文の抜粋」を返させ、その抜粋が本文に
実在するかを部分文字列で照合する。LLM に反映の可否を判定させない。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import re

# 抜粋がこれより短いと偶然一致してしまうため未反映として扱う
MIN_EXCERPT_LEN = 8


def _normalize(text: str) -> str:
    """照合のために空白を全て取り除く。"""
    return re.sub(r"\s+", "", text)


def strip_html(body: str) -> str:
    """HTML タグを除去し、空白を取り除いた平文を返す。"""
    return _normalize(re.sub(r"<[^>]+>", " ", body))


def _health_labels(health_data: dict) -> list:
    """健康データのうち値がある項目だけをラベル化する。"""
    labels = []
    if health_data.get("steps") is not None:
        labels.append(f"健康: 歩数 {health_data['steps']:,}歩")
    if health_data.get("sleep_minutes") is not None:
        h, m = divmod(health_data["sleep_minutes"], 60)
        labels.append(f"健康: 睡眠時間 {h}時間{m}分" if m else f"健康: 睡眠時間 {h}時間")
    if health_data.get("distance_km"):
        labels.append(f"健康: 移動距離 {health_data['distance_km']}km")
    if health_data.get("calories"):
        labels.append(f"健康: 消費カロリー 約{health_data['calories']:,}kcal")
    if health_data.get("active_minutes"):
        labels.append(f"健康: アクティブ時間 {health_data['active_minutes']}分")
    if health_data.get("exercises"):
        labels.append("健康: 運動 " + "、".join(health_data["exercises"]))
    return labels


def build_labeled_items(messages: list, gcal_events: list, github_activity: list,
                        url_summaries: list, location_names: list,
                        movie_infos: list, health_data: dict) -> dict:
    """記事に必ず反映すべきデータ項目に ID を振った辞書を作る。"""
    items: dict = {}
    for i, m in enumerate(messages, 1):
        items[f"M{i}"] = f"メモ: {m}"
    for i, e in enumerate(gcal_events, 1):
        items[f"C{i}"] = f"予定: {e}"
    for i, a in enumerate(github_activity, 1):
        items[f"G{i}"] = f"GitHub: {a}"
    for i, s in enumerate(url_summaries, 1):
        items[f"U{i}"] = f"共有URL: {s['title'] or s['url']}"
    for i, n in enumerate(location_names, 1):
        items[f"L{i}"] = f"訪問場所: {n}"
    for i, v in enumerate(movie_infos, 1):
        items[f"V{i}"] = f"映画: {v['title']}"
    for i, label in enumerate(_health_labels(health_data or {}), 1):
        items[f"H{i}"] = label
    return items


def find_missing(items: dict, body: str, coverage_map) -> list:
    """記事本文に反映されていない項目のラベル一覧を返す。

    抜粋が無い・短すぎる・本文に実在しない場合はいずれも未反映とする。
    """
    if not items:
        return []
    plain = strip_html(body)
    cov = coverage_map if isinstance(coverage_map, dict) else {}
    missing = []
    for key, label in items.items():
        excerpt = cov.get(key)
        if not isinstance(excerpt, str):
            missing.append(label)
            continue
        norm = _normalize(excerpt)
        if len(norm) < MIN_EXCERPT_LEN or norm not in plain:
            missing.append(label)
    return missing
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `py -m unittest discover -s tests -v`
Expected: PASS（合計 42 tests）

- [ ] **Step 5: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
feat: 反映漏れを機械照合する coverage モジュールを追加
```

対象ファイル: `coverage.py`, `tests/test_coverage.py`

---

### Task 7: post.py の生成側を coverage 方式へ切り替える

**Files:**
- Modify: `post.py`（`build_data_items()` と `check_article_coverage()` を削除、`format_with_gemini()` のプロンプト変更、`main()` の検証処理の差し替え）

**Interfaces:**
- Consumes: `coverage.build_labeled_items` / `coverage.find_missing`（Task 6）
- Produces: `format_with_gemini(...)` の戻り値に `coverage` キー（`dict[str, str]`）が加わる

- [ ] **Step 1: import を追加する**

`post.py` の import 群（Task 5 で追加した `import collect` の隣）に追加する。

```python
import coverage
```

- [ ] **Step 2: 不要になった関数を削除する**

`post.py:897` の `build_data_items()` と `post.py:930` の `check_article_coverage()` を関数ごと削除する。前者は `coverage.build_labeled_items()` に、後者は `coverage.find_missing()` に置き換わる。

- [ ] **Step 3: 各セクションを ID 付きにする**

`format_with_gemini()` 内の `extra_sections` の組み立てを ID 付きに変更する。ID の採番順は `coverage.build_labeled_items()` と完全に一致させること。

```python
    if gcal_events:
        lines = [f"[C{i}] {e}" for i, e in enumerate(gcal_events, 1)]
        extra_sections += "\n【今日の予定】\n" + "\n".join(lines) + "\n"
    if github_activity:
        lines = [f"[G{i}] {a}" for i, a in enumerate(github_activity, 1)]
        extra_sections += "\n【GitHub 活動】\n" + "\n".join(lines) + "\n"
```

共有URL・訪問場所・映画のセクションも同様に差し替える。

```python
    if url_summaries:
        lines = []
        for i, s in enumerate(url_summaries, 1):
            line = f"[U{i}] タイトル: {s['title']}"
            if s["description"]:
                line += f"\n  紹介: {s['description']}"
            line += f"\n  URL: {s['url']}"
            lines.append(line)
        extra_sections += "\n【共有URL】\n" + "\n".join(lines) + "\n"
    if location_names:
        lines = [f"[L{i}] {n}" for i, n in enumerate(location_names, 1)]
        extra_sections += "\n【訪問場所】\n" + "\n".join(lines) + "\n"
    if movie_infos:
        lines = []
        for i, m in enumerate(movie_infos, 1):
            line = f"[V{i}] {m['title']}"
            if m.get("runtime"):
                line += f"（{m['runtime']}）"
            if m.get("genres"):
                line += f" [{m['genres']}]"
            if m.get("overview"):
                line += f" — {m['overview']}"
            lines.append(line)
        extra_sections += "\n【鑑賞した映画】\n" + "\n".join(lines) + "\n"
```

健康データは行を組み立てた後にまとめて番号を振る（`coverage._health_labels()` と同じ順序で追加されているため対応が取れる）。

```python
        if lines:
            lines = [f"[H{i}] {line.lstrip('・')}" for i, line in enumerate(lines, 1)]
            extra_sections += "\n【健康データ（Fitbit）】\n" + "\n".join(lines) + "\n"
```

メモのセクションも ID 付きにする。

```python
    if has_memo:
        combined = "\n".join(f"[M{i}] {m}" for i, m in enumerate(messages, 1))
        memo_section = f"\n【メモ】\n{combined}\n"
```

- [ ] **Step 4: 出力形式に coverage を追加する**

プロンプト末尾の JSON 形式の指定を差し替える。

```python
- 必ず以下の JSON 形式のみで返す（コードブロック不要）:
{{"title": "記事タイトル", "body": "<h3>...</h3><p>...</p>...<p><b>本日のよかったこと</b></p><ul><li>...</li></ul>", "coverage": {{"M1": "本文からそのまま写した抜粋", "C1": "..."}}}}
```

`【絶対に守るルール】` の末尾（`{feedback_section}` の直前）に以下を追加する。

```python
- 各データ項目の先頭にある [M1] [C1] のような ID は本文には書かない。本文に ID を含めてはならない
- coverage には、提供された全ての ID をキーとして必ず含める。値はその項目に触れている箇所を
  本文から連続する15文字以上そのまま写した文字列にする
  - 本文に存在しない文字列を coverage に書いてはならない。要約・言い換えも不可
  - 該当箇所が本文に無い場合は、まず本文にその内容を書き足してから抜粋を写すこと
```

- [ ] **Step 5: main() の検証処理を差し替える**

`post.py` の「3.5 反映漏れチェック」ブロック全体（`data_items = build_data_items(` から `body += f"<p><b>そのほかの記録</b></p><ul>{lis}</ul>"` まで）を以下に置き換える。

```python
            # 3.5 反映漏れチェック: 各データ項目に対応する本文の抜粋を Gemini に返させ、
            #     その抜粋が本文に実在するかを機械的に照合する。漏れがあれば再生成（最大2回）。
            #     それでも漏れたら記事末尾に追記する。
            data_items = coverage.build_labeled_items(
                messages, gcal_events, github_activity,
                url_summaries, location_names, movie_infos, health_data,
            )
            missing = coverage.find_missing(data_items, body, article.get("coverage"))
            for retry in range(1, 3):
                if not missing:
                    break
                print(f"⚠️  {len(missing)} 件が記事に未反映。再生成します ({retry}/2)...")
                for x in missing:
                    print(f"   - {x[:60]}")
                article = format_with_gemini(
                    messages, weather, github_activity, gcal_events,
                    url_summaries, location_names, movie_infos, news_headlines,
                    health_data=health_data,
                    week_posts=week_posts,
                    target_date=target_date,
                    missing_feedback=missing,
                )
                title = article["title"]
                body  = article["body"]
                missing = coverage.find_missing(data_items, body, article.get("coverage"))
            if missing:
                print(f"⚠️  再生成後も {len(missing)} 件が未反映のため、記事末尾に追記します")
                lis = "".join(f"<li>{html.escape(x)}</li>" for x in missing)
                body += f"<p><b>そのほかの記録</b></p><ul>{lis}</ul>"
```

- [ ] **Step 6: 構文とテストを確認する**

Run: `py -c "import ast,io; ast.parse(io.open('post.py',encoding='utf-8').read()); print('構文OK')"`
Expected: `構文OK`

Run: `py -c "import io; s=io.open('post.py',encoding='utf-8').read(); assert 'check_article_coverage' not in s and 'build_data_items' not in s; print('旧関数の削除OK')"`
Expected: `旧関数の削除OK`

Run: `py -m unittest discover -s tests -v`
Expected: PASS（合計 42 tests）

- [ ] **Step 7: 手動実行で動作を確認する**

GitHub の Actions タブ → 「Daily Blog Post」→ Run workflow で実行する。

Expected:
- ログに「⚠️ N 件が記事に未反映」が出ないか、出ても再生成後に解消していること
- 投稿された記事にメモの全項目が反映されていること
- 記事本文に `[M1]` のような ID が混入していないこと

本文に ID が混入した場合は Step 4 のルール文言を強める。毎回 3 回とも未反映が残る場合は、ログの `missing` 内容を見て「15文字以上」の指定と `coverage.MIN_EXCERPT_LEN` が厳しすぎないかを判断する。

- [ ] **Step 8: コミット（ユーザーが手動で実行）**

コミットメッセージ案:

```
refactor: 反映漏れ検証を機械照合に置き換え Gemini 呼び出しを削減
```

対象ファイル: `post.py`

---

## 完了後の運用確認

数日運用したうえで以下を確認する。

- 「Collect Telegram Memos」ワークフローが 2 時間ごとに緑で完了しているか
- Gist の `memoryex-state.json` に当日分のメモが揃っているか（Telegram に送った件数と一致するか）
- 「Daily Blog Post」のログで `[診断] 保存済みメモ` の件数が期待どおりか
- 記事末尾に「そのほかの記録」が出ていないか（出ていれば生成側でまだ漏れている）
