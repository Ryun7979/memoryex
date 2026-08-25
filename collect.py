"""Telegram のメモを収集して Secret Gist に保存するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import gist_store
import notify

JST = timezone(timedelta(hours=9))

# 収集対象の更新種別。編集されたメモも拾えるよう edited_* を含める
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post"]

# getUpdates の long polling 待ち時間（秒）。更新が既にあれば待たずに返る
LONG_POLL_SECONDS = 10


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


def fetch_updates(token: str, offset: int, limit: int = 100) -> list:
    """Telegram から未確定の更新を取得する。

    offset を渡した時点でそれより前の更新は確定（confirm）され、
    サーバー側のキューから外れる。保存済みの範囲のみを確定させること。

    timeout=0（即時リターン）だと、長時間ポーリングされていなかった bot への
    最初の呼び出しで、サーバーがキューを用意しきる前に空で返ることがある。
    23:00 の投稿直前の収集で空振りすると、その日の記事に直接響くため
    long polling で待つ。更新が既にあれば待たずに即座に返る。
    """
    params = {
        "limit": str(limit),
        "timeout": str(LONG_POLL_SECONDS),
        "allowed_updates": json.dumps(ALLOWED_UPDATES),
    }
    if offset > 0:
        params["offset"] = str(offset)
    url = (f"https://api.telegram.org/bot{token}/getUpdates?"
           + urllib.parse.urlencode(params))
    # HTTP 側のタイムアウトは long polling の待ち時間より必ず長くする
    with urllib.request.urlopen(url, timeout=LONG_POLL_SECONDS + 20) as res:
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
