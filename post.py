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
GEMINI_MODELS    = ["gemini-2.5-flash"]
DEBUG            = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")

WEATHER_LOCATION   = os.environ.get("WEATHER_LOCATION", "")
GITHUB_USERNAME    = os.environ.get("GITHUB_USERNAME", "")
GH_API_TOKEN       = os.environ.get("GH_API_TOKEN", "")
GCAL_CLIENT_ID     = os.environ.get("GCAL_CLIENT_ID", "")
GCAL_CLIENT_SECRET = os.environ.get("GCAL_CLIENT_SECRET", "")
GCAL_REFRESH_TOKEN = os.environ.get("GCAL_REFRESH_TOKEN", "")
# ────────────────────────────────────────────────────────


def get_weather(location: str) -> str:
    """wttr.in から今日の天気を取得する（APIキー不要）。"""
    if not location:
        return ""
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
        c = data["current_condition"][0]
        desc = c["weatherDesc"][0]["value"]
        temp = c["temp_C"]
        feels = c["FeelsLikeC"]
        return f"{location}: {desc}, {temp}℃（体感 {feels}℃）"
    except Exception as e:
        print(f"  [WARN] 天気取得失敗: {e}")
        return ""


def get_github_activity(username: str, token: str = "") -> list[str]:
    """GitHub API から今日（JST）のアクティビティを取得する。"""
    if not username:
        return []
    url = f"https://api.github.com/users/{username}/events?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "memoryex"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as res:
            events = json.loads(res.read())
    except Exception as e:
        print(f"  [WARN] GitHub アクティビティ取得失敗: {e}")
        return []

    today = datetime.now(JST).date()
    activities: list[str] = []
    seen: set[str] = set()

    for event in events:
        created_at = datetime.strptime(
            event["created_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).astimezone(JST)
        if created_at.date() != today:
            continue

        etype = event.get("type", "")
        repo = event.get("repo", {}).get("name", "").split("/")[-1]
        payload = event.get("payload", {})

        if etype == "PushEvent":
            for c in payload.get("commits", []):
                msg = c.get("message", "").splitlines()[0]
                key = f"push:{repo}:{msg}"
                if key not in seen:
                    activities.append(f"{repo} にコミット: {msg}")
                    seen.add(key)
        elif etype == "PullRequestEvent":
            pr = payload.get("pull_request", {})
            action = payload.get("action", "")
            title = pr.get("title", "")
            key = f"pr:{repo}:{title}:{action}"
            if key not in seen:
                activities.append(f"{repo} の PR 「{title}」を {action}")
                seen.add(key)
        elif etype == "IssuesEvent":
            issue = payload.get("issue", {})
            action = payload.get("action", "")
            title = issue.get("title", "")
            key = f"issue:{repo}:{title}:{action}"
            if key not in seen:
                activities.append(f"{repo} の Issue 「{title}」を {action}")
                seen.add(key)
        elif etype == "CreateEvent":
            ref_type = payload.get("ref_type", "")
            ref = payload.get("ref", "")
            key = f"create:{repo}:{ref}"
            if key not in seen:
                activities.append(f"{repo} に {ref_type} 「{ref}」を作成")
                seen.add(key)

    return activities


def get_gcal_events(client_id: str, client_secret: str, refresh_token: str) -> list[str]:
    """Google Calendar API から今日（JST）の予定を取得する。"""
    if not all([client_id, client_secret, refresh_token]):
        return []

    # アクセストークンを取得
    token_payload = json.dumps({
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }).encode()
    try:
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            access_token = json.loads(res.read())["access_token"]
    except Exception as e:
        print(f"  [WARN] Google Calendar トークン取得失敗: {e}")
        return []

    # 今日の開始・終了を ISO 8601 で生成
    today_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    params = urllib.parse.urlencode({
        "timeMin":       today_start.isoformat(),
        "timeMax":       today_end.isoformat(),
        "singleEvents":  "true",
        "orderBy":       "startTime",
    })
    try:
        req = urllib.request.Request(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            items = json.loads(res.read()).get("items", [])
    except Exception as e:
        print(f"  [WARN] Google Calendar イベント取得失敗: {e}")
        return []

    events: list[str] = []
    for item in items:
        summary = item.get("summary", "(無題)")
        start = item.get("start", {})
        if "dateTime" in start:
            dt = datetime.fromisoformat(start["dateTime"])
            events.append(f"{dt.strftime('%H:%M')} {summary}")
        else:
            events.append(f"終日 {summary}")
    return events


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


def format_with_gemini(
    messages: list[str],
    weather: str = "",
    github_activity: list[str] = [],
    gcal_events: list[str] = [],
) -> dict:
    """Gemini API でメモをブログ記事に整形する。"""
    combined = "\n".join(f"・{m}" for m in messages)
    today_str = datetime.now(JST).strftime("%Y年%-m月%-d日")

    # 追加情報セクションを構築
    extra_sections = ""
    if weather:
        extra_sections += f"\n【天気】\n{weather}\n"
    if gcal_events:
        extra_sections += "\n【今日の予定】\n" + "\n".join(f"・{e}" for e in gcal_events) + "\n"
    if github_activity:
        extra_sections += "\n【GitHub 活動】\n" + "\n".join(f"・{a}" for a in github_activity) + "\n"

    prompt = f"""\
以下は{today_str}の日常メモと補足情報です。これをブログ記事としてまとめてください。

【要件】
- メモに書かれたことを端的にそのまま書く。説明・感想・まとめの付け足しはしない
- 補足情報（天気・予定・GitHub）は自然に本文へ織り込む。ただし全て無理に入れなくてよい
- ポジティブな出来事は少しだけ前向きに表現してよいが、大げさにしない
- 一文は短く。冗長な表現・装飾・接続詞の多用は避ける
- タイトルは 20 字以内で簡潔に
- 本文は <p> タグで段落を区切る
- 本文の末尾に「本日のよかったこと」セクションを必ず追加する
  - メモ・補足情報からポジティブな要素を 3 つ選び、なければ小さなことでも前向きに解釈して補う
  - 形式: <p><b>本日のよかったこと</b></p><ul><li>...</li><li>...</li><li>...</li></ul>
- マークダウン不可
- 必ず以下の JSON 形式のみで返す（コードブロック不要）:
{{"title": "記事タイトル", "body": "<p>本文...</p><p><b>本日のよかったこと</b></p><ul><li>...</li></ul>"}}

【メモ】
{combined}
{extra_sections}"""

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4}
    }).encode()

    last_error = None
    data = None
    for model in GEMINI_MODELS:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        print(f"  Gemini モデル: {model}")
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
                last_error = RuntimeError(f"Gemini API エラー {e.code}: {err_body}")
                break
        if data is not None:
            break
        print(f"  {model} が利用不可。次のモデルへフォールバック...")
    if data is None:
        raise last_error

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


def notify_telegram(title: str, body: str, blog_url: str) -> None:
    """ブログ記事の内容を Telegram に通知する（AI 不使用・HTML タグを平文変換）。"""
    # <li> を箇条書き記号に、<p>/<br> を改行に変換してからタグを除去
    text = body
    text = re.sub(r'<li[^>]*>', '・', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    message = f"📝 {title}\n\n{text}\n\n🔗 {blog_url}"

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    message,
    }).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            result = json.loads(res.read())
        if not result.get("ok"):
            print(f"  [WARN] Telegram 通知失敗: {result}")
    except urllib.error.URLError as e:
        print(f"  [WARN] Telegram 通知エラー: {e}")


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

        # 2. 補足情報を収集
        print("\n🌤️  補足情報を収集中...")
        weather = get_weather(WEATHER_LOCATION)
        if weather:
            print(f"   天気: {weather}")
        github_activity = get_github_activity(GITHUB_USERNAME, GH_API_TOKEN)
        if github_activity:
            print(f"   GitHub: {len(github_activity)} 件")
        gcal_events = get_gcal_events(GCAL_CLIENT_ID, GCAL_CLIENT_SECRET, GCAL_REFRESH_TOKEN)
        if gcal_events:
            print(f"   カレンダー: {len(gcal_events)} 件")

        # 3. Gemini で整形
        print("\n🤖 Gemini で記事を生成中...")
        article = format_with_gemini(messages, weather, github_activity, gcal_events)
        title = article["title"]
        body  = article["body"]
        print(f"✅  タイトル: {title}")

    # 3. JUGEM に投稿
    print("\n📝 JUGEM に投稿中...")
    post_id = post_to_jugem(title, body)
    blog_url = f"https://nadaryu.jugem.cc/?eid={post_id}"
    print(f"✅  投稿完了！ post_id = {post_id}")
    print(f"   URL: {blog_url}")

    # 4. Telegram に記事内容を通知
    print("\n📨 Telegram に通知中...")
    notify_telegram(title, body, blog_url)
    print("✅  Telegram 通知完了")


if __name__ == "__main__":
    main()
