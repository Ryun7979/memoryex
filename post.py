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
DEBUG            = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")
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
            if e.code in (429, 503) and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"  Gemini {e.code} 一時エラー。{wait}秒待機後リトライ ({attempt+1}/2)...")
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
            raw = res.read()
            ct = res.headers.get("Content-Type", "")
            if "shift_jis" in ct.lower() or "shift-jis" in ct.lower():
                text = raw.decode("shift_jis", errors="replace")
            elif "euc" in ct.lower():
                text = raw.decode("euc-jp", errors="replace")
            else:
                text = raw.decode("utf-8", errors="ignore")
            if DEBUG:
                print(f"  [DEBUG] GET {url}")
                print(f"  [DEBUG]   → final: {res.geturl()}")
                print(f"  [DEBUG]   → Content-Type: {ct}")
            return text, res.geturl()

    def do_post(url, params, encoding="utf-8", extra_headers=None):
        data = urllib.parse.urlencode(params, encoding=encoding).encode('ascii')
        h = {
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        }
        if extra_headers:
            h.update(extra_headers)
        if DEBUG:
            print(f"  [DEBUG] POST {url}")
            print(f"  [DEBUG]   encoding={encoding}, keys={list(params.keys())}")
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with opener.open(req, timeout=20) as res:
            raw = res.read()
            ct = res.headers.get("Content-Type", "")
            if "shift_jis" in ct.lower() or "shift-jis" in ct.lower():
                text = raw.decode("shift_jis", errors="replace")
            elif "euc" in ct.lower():
                text = raw.decode("euc-jp", errors="replace")
            else:
                text = raw.decode("utf-8", errors="ignore")
            if DEBUG:
                print(f"  [DEBUG]   → final: {res.geturl()}")
                print(f"  [DEBUG]   → Content-Type: {ct}")
                print(f"  [DEBUG]   → body[:500]: {text[:500]!r}")
            return text, res.geturl()

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
    if DEBUG:
        print(f"  [DEBUG] csrf_token={'(取得済み)' if csrf_token else '(未取得)'}")

    # ── 2. jugem.jp/login に POST してログイン（jugem.jp は UTF-8）──
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
        encoding="utf-8",
        extra_headers={"Referer": "https://jugem.jp/login"},
    )
    if "jugem.jp/login" in final:
        raise RuntimeError(f"JUGEM ログイン失敗（ログインページに留まった）: {final}")
    print(f"  → ログイン成功: {final}")

    # ── 3. 記事投稿フォームを取得（rich view: csrf_token が含まれる）──
    manage_base = final.split("?")[0]  # https://nadaryu.jugem.cc/manage/
    entry_html, entry_final = do_get(f"{manage_base}?mode=entry&view=rich")
    if "jugem.jp/login" in entry_final:
        raise RuntimeError(f"セッション未確立: {entry_final}")
    print(f"  → 投稿フォームURL: {entry_final}")

    # フォームの action を取得
    form_action_m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', entry_html, re.IGNORECASE)
    form_action = urllib.parse.urljoin(entry_final, form_action_m.group(1) if form_action_m else "")

    # フォームのエンコーディングを meta charset から推定
    charset_m = re.search(r'charset=["\']?([^"\'\s;>]+)', entry_html, re.IGNORECASE)
    form_encoding = "utf-8"
    if charset_m:
        cs = charset_m.group(1).lower().replace("-", "_")
        if "shift_jis" in cs or "sjis" in cs:
            form_encoding = "shift_jis"
        elif "euc_jp" in cs or "euc-jp" in cs:
            form_encoding = "euc-jp"

    # form_action の URL から view= パラメータを除去する
    # （view=rich はエディタ表示用であり、POST 時に残っていると insert が無視される）
    parsed = urllib.parse.urlparse(form_action)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    qs.pop('view', None)
    form_action = parsed._replace(query=urllib.parse.urlencode(qs, doseq=True)).geturl()

    if DEBUG:
        print(f"  [DEBUG] form_action={form_action!r}")
        print(f"  [DEBUG] form_encoding={form_encoding}")

    # hidden fields を収集（name/value 順序どちらでも対応）
    hidden = {}
    for m in re.finditer(
        r'<input[^>]+(?:type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']'
        r'|name=["\']([^"\']+)["\'][^>]*type=["\']hidden["\'][^>]*value=["\']([^"\']*)["\']'
        r'|value=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\'][^>]*type=["\']hidden["\'])',
        entry_html, re.IGNORECASE
    ):
        g = m.groups()
        if g[0]:   name, value = g[0], g[1]
        elif g[2]: name, value = g[2], g[3]
        elif g[5]: name, value = g[5], g[4]
        else: continue
        hidden[name] = value

    # select 要素のデフォルト値を収集（category_id など）
    selects = {}
    for m in re.finditer(
        r'<select[^>]+name=["\']([^"\']+)["\'][^>]*>(.*?)</select>',
        entry_html, re.IGNORECASE | re.DOTALL
    ):
        sel_name = m.group(1)
        sel_body = m.group(2)
        # selected="selected" のオプションを優先
        sel_m = re.search(r'<option[^>]+value=["\']([^"\']*)["\'][^>]*selected', sel_body, re.IGNORECASE)
        if not sel_m:
            # なければ最初のオプション
            sel_m = re.search(r'<option[^>]+value=["\']([^"\']*)["\']', sel_body, re.IGNORECASE)
        selects[sel_name] = sel_m.group(1) if sel_m else ""

    if DEBUG:
        print(f"  [DEBUG] hidden: {hidden}")
        print(f"  [DEBUG] selects: {selects}")

    # POST 前のフォーム HTML に含まれる eid を記録（比較用）
    pre_eids = set(re.findall(r'eid=(\d+)', entry_html))

    # ── 4. 全フィールド + action=insert で POST ──
    insert_params = {
        **hidden,
        # select フィールド（category_id など）を個別に追加（hidden との重複を避ける）
        "category_id": selects.get("category_id", "0"),
        "theme_id":    selects.get("theme_id", "0"),
        "accept_cm":   selects.get("accept_cm", "0"),
        "ping_state":  selects.get("ping_state", "1"),
        "title":       title,
        "description": body,
        "sequel":      "",
        "state":       "1",   # 1=公開, 0=下書き（JUGEM の実際の仕様に合わせる）
        "set_date":    "0",
        "action":      "insert",
    }
    done_html, done_final = do_post(
        form_action,
        insert_params,
        encoding=form_encoding,
        extra_headers={"Referer": entry_final},
    )
    print(f"  → POST 完了 URL: {done_final}")

    # 成功判定1: リダイレクト先 URL に eid が含まれている場合
    eid_m = re.search(r'eid=(\d+)', done_final)
    if eid_m:
        return eid_m.group(1)

    # 成功判定2: JUGEM はフォームを再表示する場合がある
    # POST 後の HTML に現れた新規 eid（= POST 前に存在しなかった eid）を新記事とみなす
    post_eids = set(re.findall(r'eid=(\d+)', done_html))
    new_eids = post_eids - pre_eids
    if DEBUG:
        print(f"  [DEBUG] pre_eids={sorted(pre_eids, key=int)}")
        print(f"  [DEBUG] post_eids={sorted(post_eids, key=int)}")
        print(f"  [DEBUG] new_eids={sorted(new_eids, key=int)}")
    if new_eids:
        new_eid = max(new_eids, key=int)
        print(f"  → 新規 eid 検出: {new_eid}")
        return new_eid

    raise RuntimeError(f"JUGEM 投稿失敗: 新規 eid を検出できませんでした (URL={done_final})")

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
