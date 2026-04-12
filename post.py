"""
Telegram の今日のメモを Gemini で整形して JUGEM ブログに投稿するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
JUGEM_USER       = os.environ["JUGEM_USER"]
JUGEM_PASS       = os.environ["JUGEM_PASS"]

JST              = timezone(timedelta(hours=9))
JUGEM_ATOM_URL   = "https://nadaryu.jugem.cc/atom/entry/"
GEMINI_MODEL     = "gemini-2.5-flash"
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
    """JUGEM AtomPub API で記事を投稿する（Basic認証）。"""
    import re

    credentials = base64.b64encode(
        f"{JUGEM_USER}:{JUGEM_PASS}".encode()
    ).decode()
    auth_header = f"Basic {credentials}"

    def api_get(url):
        req = urllib.request.Request(url, headers={
            "Authorization": auth_header,
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read().decode("utf-8", errors="ignore")

    # ── 1. サービスドキュメントからエントリ投稿URLを自動取得 ──
    collection_url = None
    for svc_url in [JUGEM_ATOM_URL, f"https://nadaryu.jugem.cc/atom/"]:
        try:
            svc_doc = api_get(svc_url)
            print(f"  → サービスドキュメント取得: {svc_url}")
            print(f"  → 先頭500字: {svc_doc[:500]}")
            # <collection href="..."> を探す
            m = re.search(r'<collection[^>]+href=["\']([^"\']+)["\']', svc_doc)
            if m:
                collection_url = m.group(1)
                print(f"  → collectionURL: {collection_url}")
                break
        except urllib.error.HTTPError as e:
            print(f"  → {svc_url} → HTTP {e.code}")
        except Exception as e:
            print(f"  → {svc_url} → エラー: {e}")

    # サービスドキュメントで見つからなければ既知URLを試す
    if not collection_url:
        collection_url = JUGEM_ATOM_URL
        print(f"  → サービスドキュメント未取得。フォールバック: {collection_url}")

    # ── 2. Atom エントリを投稿 ──
    now_str = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    atom_entry = f"""<?xml version="1.0" encoding="UTF-8"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <title>{_xml_escape(title)}</title>
  <content type="html">{_xml_escape(body)}</content>
  <updated>{now_str}</updated>
  <app:control xmlns:app="http://www.w3.org/2007/app">
    <app:draft>no</app:draft>
  </app:control>
</entry>"""

    req = urllib.request.Request(
        collection_url,
        data=atom_entry.encode("utf-8"),
        headers={
            "Content-Type": "application/atom+xml; charset=utf-8",
            "Authorization": auth_header,
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            resp_body = res.read().decode("utf-8", errors="ignore")
            print(f"  → AtomPub POST ステータス: {res.status}")
            print(f"  → レスポンス先頭300字: {resp_body[:300]}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"AtomPub 投稿失敗 HTTP {e.code}: {err_body[:300]}"
        )

    eid_m = re.search(r'eid=(\d+)|/entry/(\d+)', resp_body)
    return eid_m.group(1) or eid_m.group(2) if eid_m else "ok"


def _xml_escape(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))

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
