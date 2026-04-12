"""
Telegram の今日のメモを Gemini で整形して JUGEM ブログに投稿するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import os
import re
import json
import time
import http.cookiejar
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
JUGEM_USER       = os.environ["JUGEM_USER"]
JUGEM_PASS       = os.environ["JUGEM_PASS"]

JST              = timezone(timedelta(hours=9))
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                data = json.loads(res.read())
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            if e.code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"  Gemini 429 レート制限。{wait}秒待機後リトライ ({attempt+1}/2)...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini API エラー {e.code}: {err_body}")

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
    """JUGEM ブログ管理画面フォームで記事を投稿する。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0.0.0 Safari/537.36")

    def do_get(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        })
        with opener.open(req, timeout=20) as res:
            return res.read().decode("utf-8", errors="ignore"), res.geturl()

    def do_post(url, params, extra_headers=None):
        data = urllib.parse.urlencode(params).encode()
        h = {
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        }
        if extra_headers:
            h.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with opener.open(req, timeout=20) as res:
            return res.read().decode("utf-8", errors="ignore"), res.geturl()

    # ── 1. jugem.jp/login を GET して CSRF トークンを取得 ──
    login_html, _ = do_get("https://jugem.jp/login")
    token_m = (
        re.search(r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']', login_html)
        or re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']', login_html)
    )
    if token_m:
        csrf_token = token_m.group(1)
    else:
        csrf_token = None
        for c in jar:
            if c.name == "XSRF-TOKEN":
                csrf_token = urllib.parse.unquote(c.value)
                break

    # ── 2. jugem.jp/login に POST してログイン ──
    # フォームフィールド: _token, account_name, password, is_sub_user, redirect_url, isSavePass
    print("  → JUGEM ログイン中...")
    resp, final = do_post(
        "https://jugem.jp/login",
        {
            "_token":       csrf_token or "",
            "account_name": JUGEM_USER,
            "password":     JUGEM_PASS,
            "is_sub_user":  "0",
            "redirect_url": "",
            "isSavePass":   "0",
        },
        extra_headers={"Referer": "https://jugem.jp/login"},
    )
    if "jugem.jp/login" in final:
        raise RuntimeError(f"JUGEM ログイン失敗（ログインページに留まった）: {final}")
    print(f"  → ログイン成功: {final}")

    # ── 3. 記事投稿フォームを取得 ──
    manage_base = final.split("?")[0]  # https://nadaryu.jugem.cc/manage/
    entry_html, entry_final = do_get(f"{manage_base}?mode=entry")
    if "jugem.jp/login" in entry_final:
        raise RuntimeError(f"セッション未確立: {entry_final}")

    # フォームの action / hidden fields を収集
    form_action_m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', entry_html, re.IGNORECASE)
    form_action = urllib.parse.urljoin(entry_final, form_action_m.group(1) if form_action_m else "")
    hidden = {}
    for m in re.finditer(
        r'<input[^>]+(?:type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']'
        r'|name=["\']([^"\']+)["\'][^>]*type=["\']hidden["\'][^>]*value=["\']([^"\']*)["\'])',
        entry_html, re.IGNORECASE
    ):
        name  = m.group(1) or m.group(3)
        value = m.group(2) if m.group(2) is not None else m.group(4)
        if name:
            hidden[name] = value

    # 現在のJST日時をフォームに明示的にセット
    now = datetime.now(JST)

    # ── 4. 確認ページを経由して記事を公開 ──
    # JUGEM は confirm → insert の2段階。confirm だけでは下書き状態。
    post_params = {
        **hidden,
        "title":       title,
        "description": body,
        "mode":        "write",
        "action":      "confirm",
        "state":       "1",          # 1=公開, 2=下書き
        "year":        str(now.year),
        "month":       str(now.month).zfill(2),
        "day":         str(now.day).zfill(2),
        "hour":        str(now.hour).zfill(2),
        "minute":      str(now.minute).zfill(2),
    }
    print(f"  → 確認ページ送信: {form_action}")
    print(f"  → hidden fields: {list(hidden.keys())}")
    conf_html, conf_final = do_post(
        form_action,
        post_params,
        extra_headers={"Referer": entry_final},
    )
    print(f"  → 確認後URL: {conf_final}")
    print(f"  → 確認ページ冒頭: {conf_html[:500]}")

    # 確認ページの hidden fields を取得して insert（公開）
    conf_hidden = dict(re.findall(
        r'<input[^>]+name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
        conf_html, re.IGNORECASE
    ))
    print(f"  → confirm hidden fields: {list(conf_hidden.keys())}")
    # state が confirm から引き継がれない場合は上書き
    conf_hidden.setdefault("state", "1")
    print(f"  → 公開送信 (action=insert): {form_action}")
    done_html, done_final = do_post(
        form_action,
        {**conf_hidden, "title": title, "description": body, "mode": "write", "action": "insert"},
        extra_headers={"Referer": conf_final},
    )
    print(f"  → 公開後URL: {done_final}")
    print(f"  → 公開後レスポンス冒頭: {done_html[:500]}")

    eid_m = re.search(r'eid=(\d+)', done_final + done_html)
    return eid_m.group(1) if eid_m else "ok"

def main():
    print(f"=== 実行開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')} ===")

    test_mode = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

    if test_mode:
        print("🔧 TEST_MODE: Telegram/Gemini をスキップして固定文字列で投稿します。")
        title = "テスト投稿"
        body  = "<p>これは接続確認用のテスト投稿です。自動投稿スクリプトから送信されました。</p>"
    else:
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
        title = article["title"]
        body  = article["body"]
        print(f"✅  タイトル: {title}")

    # 3. JUGEM に投稿
    print("\n📝 JUGEM に投稿中...")
    post_id = post_to_jugem(title, body)
    print(f"✅  投稿完了！ post_id = {post_id}")
    print(f"   URL: https://nadaryu.jugem.cc/?eid={post_id}")


if __name__ == "__main__":
    main()
