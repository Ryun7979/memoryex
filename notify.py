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
