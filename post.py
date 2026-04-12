"""
Telegram の今日のメモを Gemini で整形して JUGEM ブログに投稿するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import os
import json
import urllib.request
import urllib.error
import xmlrpc.client
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
JUGEM_USER       = os.environ["JUGEM_USER"]
JUGEM_PASS       = os.environ["JUGEM_PASS"]

JST              = timezone(timedelta(hours=9))
JUGEM_XMLRPC_URL = "https://jugem.jp/xmlrpc/"   # JUGEM の XML-RPC エンドポイント
JUGEM_BLOG_ID    = "1"                           # 通常は "1"（複数ブログなら要変更）
GEMINI_MODEL = "gemini-2.5-flash"
# ────────────────────────────────────────────────────────


def get_today_messages() -> list[str]:
    """Telegram から今日（JST）送ったメッセージを取得する。"""
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        "/getUpdates?limit=100&allowed_updates=[\"message\"]"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as res:
            data = json.loads(res.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram API 接続エラー: {e}")

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API エラー: {data}")

    today = datetime.now(JST).date()
    messages: list[str] = []

    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post", {})
        if not msg:
            continue
        # chat_id が一致するメッセージのみ
        if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
            continue
        # 今日のメッセージのみ
        ts = datetime.fromtimestamp(msg["date"], tz=JST)
        if ts.date() != today:
            continue
        text = msg.get("text", "").strip()
        if text:
            messages.append(text)

    return messages


def format_with_gemini(messages: list[str]) -> dict:
    """Gemini API でメモをブログ記事に整形する。"""
    combined = "\n".join(f"・{m}" for m in messages)
    today_str = datetime.now(JST).strftime("%Y年%-m月%-d日")

    prompt = f"""\
以下は{today_str}の日常メモです。これをブログ記事としてまとめてください。

【要件】
- 読みやすく自然な日本語にする
- タイトルは簡潔に
- 本文は段落ごとに改行し、HTML の <p> タグで囲む
- 箇条書きが適切なら <ul><li> を使う
- マークダウンは使わない
- 必ず以下の JSON 形式のみで返す（コードブロック不要）:
{{"title": "記事タイトル", "body": "<p>本文...</p>"}}

【メモ】
{combined}"""

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7}
    }).encode()

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini API エラー {e.code}: {e.read().decode()}")

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

    # ```json ... ``` ブロックを除去
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini レスポンスの JSON パース失敗: {e}\n{raw_text}")


def post_to_jugem(title: str, body: str) -> str:
    """JUGEM ブログに XML-RPC（MetaWeblog API）で投稿する。"""
    server = xmlrpc.client.ServerProxy(JUGEM_XMLRPC_URL, allow_none=True)

    content = {
        "title":       title,
        "description": body,
        "dateCreated": xmlrpc.client.DateTime(datetime.now(JST)),
        "mt_allow_comments": 1,
        "mt_allow_pings":    0,
    }

    try:
        post_id = server.metaWeblog.newPost(
            JUGEM_BLOG_ID, JUGEM_USER, JUGEM_PASS, content, True  # True = 公開
        )
        return str(post_id)
    except xmlrpc.client.Fault as e:
        raise RuntimeError(f"JUGEM 投稿エラー (faultCode={e.faultCode}): {e.faultString}")


def main():
    print(f"=== 実行開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')} ===")

    # 1. Telegram からメモ取得
    print("📨 Telegram からメッセージを取得中...")
    messages = get_today_messages()
    if not messages:
        print("⚠️  今日のメモが見つかりませんでした。投稿をスキップします。")
        return
    print(f"✅  {len(messages)} 件取得:")
    for i, m in enumerate(messages, 1):
        print(f"   {i}. {m[:60]}{'...' if len(m) > 60 else ''}")

    # 2. Gemini で整形
    print("\n🤖 Gemini で記事を生成中...")
    article = format_with_gemini(messages)
    print(f"✅  タイトル: {article['title']}")

    # 3. JUGEM に投稿
    print("\n📝 JUGEM に投稿中...")
    post_id = post_to_jugem(article["title"], article["body"])
    print(f"✅  投稿完了！ post_id = {post_id}")
    print(f"   URL: https://nadaryu.jugem.cc/?eid={post_id}")


if __name__ == "__main__":
    main()
