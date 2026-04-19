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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
JUGEM_USER       = os.environ["JUGEM_USER"]
JUGEM_PASS       = os.environ["JUGEM_PASS"]

JST              = timezone(timedelta(hours=9))
GEMINI_MODELS    = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
DEBUG            = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")

WEATHER_LOCATION   = os.environ.get("WEATHER_LOCATION", "")
GITHUB_USERNAME    = os.environ.get("GITHUB_USERNAME", "")
GH_API_TOKEN       = os.environ.get("GH_API_TOKEN", "")
GCAL_CLIENT_ID     = os.environ.get("GCAL_CLIENT_ID", "")
GCAL_CLIENT_SECRET = os.environ.get("GCAL_CLIENT_SECRET", "")
GCAL_REFRESH_TOKEN = os.environ.get("GCAL_REFRESH_TOKEN", "")
TMDB_API_KEY       = os.environ.get("TMDB_API_KEY", "")
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


def get_gcal_events(client_id: str, client_secret: str, refresh_token: str, target_date=None) -> list[str]:
    """Google Calendar API から指定日（JST）の予定を取得する。"""
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

    # 対象日の開始・終了を ISO 8601 で生成
    if target_date is None:
        target_date = datetime.now(JST).date()
    today_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=JST)
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


def fetch_url_summary(url: str) -> dict:
    """URL のタイトルと meta description を取得する。"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; memoryex/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            content_type = res.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return {"url": url, "title": "", "description": ""}
            raw = res.read(50000)  # 最大 50KB だけ読む
        # エンコーディング判定
        charset = "utf-8"
        for part in content_type.split(";"):
            if "charset=" in part:
                charset = part.split("=")[-1].strip()
                break
        html = raw.decode(charset, errors="replace")
        # title
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        # meta description
        m = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            html, re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
                html, re.IGNORECASE,
            )
        description = re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""
        return {"url": url, "title": title, "description": description}
    except Exception as e:
        if DEBUG:
            print(f"  [DEBUG] URL取得失敗 {url}: {e}")
        return {"url": url, "title": "", "description": ""}


def fetch_movie_info(title: str) -> dict:
    """TMDB API で映画情報を取得する。"""
    if not TMDB_API_KEY:
        return {"title": title}
    tmdb_headers = {
        "User-Agent": "memoryex/1.0",
        "Authorization": f"Bearer {TMDB_API_KEY}",
    }
    # 映画検索
    search_url = (
        f"https://api.themoviedb.org/3/search/movie"
        f"?query={urllib.parse.quote(title)}&language=ja&page=1"
    )
    try:
        req = urllib.request.Request(search_url, headers=tmdb_headers)
        with urllib.request.urlopen(req, timeout=10) as res:
            results = json.loads(res.read()).get("results", [])
        if not results:
            return {"title": title}
        movie_id = results[0]["id"]
        ja_title = results[0].get("title", title)
        # 映画詳細（上映時間・ジャンル）を取得
        detail_url = (
            f"https://api.themoviedb.org/3/movie/{movie_id}"
            f"?language=ja"
        )
        req2 = urllib.request.Request(detail_url, headers=tmdb_headers)
        with urllib.request.urlopen(req2, timeout=10) as res2:
            detail = json.loads(res2.read())
        runtime = detail.get("runtime")  # 分
        genres = [g["name"] for g in detail.get("genres", [])]
        overview = detail.get("overview", "")[:150]
        return {
            "title":    ja_title,
            "runtime":  f"{runtime}分" if runtime else "",
            "genres":   "・".join(genres) if genres else "",
            "overview": overview,
        }
    except Exception as e:
        if DEBUG:
            print(f"  [DEBUG] TMDB取得失敗 '{title}': {e}")
        return {"title": title}


def reverse_geocode(lat: float, lon: float) -> str:
    """緯度・経度から場所名を取得する（OpenStreetMap Nominatim）。"""
    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&accept-language=ja"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "memoryex/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
        addr = data.get("address", {})
        # 施設名 > 観光地 > 町名 > 区 の優先順で場所名を組み立てる
        name = (
            data.get("name")
            or addr.get("tourism")
            or addr.get("amenity")
            or addr.get("shop")
            or addr.get("leisure")
        )
        city = addr.get("city") or addr.get("town") or addr.get("village") or ""
        suburb = addr.get("suburb") or addr.get("quarter") or ""
        parts = [p for p in [name, suburb, city] if p]
        return "、".join(parts) if parts else data.get("display_name", "")[:50]
    except Exception as e:
        if DEBUG:
            print(f"  [DEBUG] 逆ジオコーディング失敗: {e}")
        return ""


def get_news_headlines(count: int = 5) -> list[dict]:
    """Yahoo!ニュース RSS から上位 n 件のヘッドラインを取得する。"""
    url = "https://news.yahoo.co.jp/rss/topics/top-picks.xml"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; memoryex/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
        root = ET.fromstring(raw)
        items: list[dict] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()
            # description に HTML タグが含まれる場合は除去
            desc  = re.sub(r"<[^>]+>", "", desc).strip()
            if title and link:
                items.append({"title": title, "url": link, "description": desc})
            if len(items) >= count:
                break
        return items
    except Exception as e:
        print(f"  [WARN] ニュース取得失敗: {e}")
        return []


def get_today_messages() -> tuple[list[str], list[dict], list[str]]:
    """Telegram から今日（JST）送ったメッセージとURL情報を取得する。"""
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

    now_jst = datetime.now(JST)
    # 深夜0〜6時に実行された場合は前日のメッセージを対象にする
    if now_jst.hour < 6:
        target_date = (now_jst - timedelta(days=1)).date()
        print(f"  （深夜実行のため前日 {target_date} のメッセージを取得）")
    else:
        target_date = now_jst.date()
    messages: list[str] = []
    url_pattern = re.compile(r"https?://\S+")
    movie_pattern = re.compile(r"^映画[　 :：、,]?\s*(.+)", re.MULTILINE)
    found_urls: list[str] = []
    found_locations: list[dict] = []
    found_movies: list[str] = []

    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post", {})
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
            continue
        ts = datetime.fromtimestamp(msg["date"], tz=JST)
        if ts.date() != target_date:
            continue
        text = msg.get("text", "").strip()
        if text:
            messages.append(text)
            for u in url_pattern.findall(text):
                if u not in found_urls:
                    found_urls.append(u)
            for m in movie_pattern.findall(text):
                t = m.strip()
                if t and t not in found_movies:
                    found_movies.append(t)
        # 位置情報メッセージを収集
        loc = msg.get("location")
        if loc:
            found_locations.append({
                "lat": loc["latitude"],
                "lon": loc["longitude"],
            })

    # URL のタイトル・概要を取得（最大5件）
    url_summaries: list[dict] = []
    for u in found_urls[:5]:
        summary = fetch_url_summary(u)
        if summary["title"] or summary["description"]:
            url_summaries.append(summary)
            if DEBUG:
                print(f"  [DEBUG] URL取得: {summary['title']} - {u}")

    # 位置情報を逆ジオコーディング
    location_names: list[str] = []
    for loc in found_locations[:5]:
        name = reverse_geocode(loc["lat"], loc["lon"])
        if name:
            location_names.append(name)
            if DEBUG:
                print(f"  [DEBUG] 位置情報: {name} ({loc['lat']}, {loc['lon']})")

    # 映画情報を TMDB から取得（最大3件）
    movie_infos: list[dict] = []
    for t in found_movies[:3]:
        info = fetch_movie_info(t)
        movie_infos.append(info)
        if DEBUG:
            print(f"  [DEBUG] 映画: {info}")

    return messages, url_summaries, location_names, movie_infos, target_date


def format_with_gemini(
    messages: list[str],
    weather: str = "",
    github_activity: list[str] = [],
    gcal_events: list[str] = [],
    url_summaries: list[dict] = [],
    location_names: list[str] = [],
    movie_infos: list[dict] = [],
    news_headlines: list[dict] = [],
    target_date=None,
) -> dict:
    """Gemini API でメモをブログ記事に整形する。"""
    if target_date is None:
        target_date = datetime.now(JST).date()
    today_str = target_date.strftime("%Y年%-m月%-d日")
    has_memo = bool(messages)

    # 追加情報セクションを構築
    extra_sections = ""
    if weather:
        extra_sections += f"\n【天気】\n{weather}\n"
    if gcal_events:
        extra_sections += "\n【今日の予定】\n" + "\n".join(f"・{e}" for e in gcal_events) + "\n"
    if github_activity:
        extra_sections += "\n【GitHub 活動】\n" + "\n".join(f"・{a}" for a in github_activity) + "\n"
    if url_summaries:
        lines = []
        for s in url_summaries:
            line = f"・タイトル: {s['title']}"
            if s["description"]:
                line += f"\n  紹介: {s['description']}"
            line += f"\n  URL: {s['url']}"
            lines.append(line)
        extra_sections += "\n【共有URL】\n" + "\n".join(lines) + "\n"
    if location_names:
        extra_sections += "\n【訪問場所】\n" + "\n".join(f"・{n}" for n in location_names) + "\n"
    if movie_infos:
        lines = []
        for m in movie_infos:
            line = f"・{m['title']}"
            if m.get("runtime"):
                line += f"（{m['runtime']}）"
            if m.get("genres"):
                line += f" [{m['genres']}]"
            if m.get("overview"):
                line += f" — {m['overview']}"
            lines.append(line)
        extra_sections += "\n【鑑賞した映画】\n" + "\n".join(lines) + "\n"
    if news_headlines:
        lines = []
        for n in news_headlines:
            line = f"・{n['title']}"
            if n.get("description"):
                line += f"\n  概要: {n['description']}"
            line += f"\n  URL: {n['url']}"
            lines.append(line)
        extra_sections += "\n【本日のニュース】\n" + "\n".join(lines) + "\n"

    if has_memo:
        combined = "\n".join(f"・{m}" for m in messages)
        memo_section = f"\n【メモ】\n{combined}\n"
        requirements = """\
- 【メモ】に含まれる内容は全て記事に反映する。一部だけを取り上げて他を省略しない
- メモの内容を自然な日記文に仕上げる。過度な装飾・大げさな表現・接続詞の多用は避ける
- 話題が変わるごとに <p> タグで段落を分け、読みやすくする
- 大きく話題が変わる箇所のみ <h3> タグでサブタイトルをつける。細かい話題ごとにはつけない
- 補足情報（天気・予定・GitHub・訪問場所・映画）は自然に本文へ織り込む。ただし全て無理に入れなくてよい
- 【共有URL】がある場合は、本文とは別に「本日の気になったインターネット」セクションを設ける
  - 形式: <p><b>本日の気になったインターネット</b></p><ul><li><a href="URL">タイトル</a> — 紹介文</li>...</ul>
- カレンダーの予定はあくまで補足。メモが主役
- カレンダーの予定に含まれる著名な会社名・組織名・個人名は、そのまま記載せず「ある会社」「ある団体」「知人」などの曖昧な表現に置き換える"""
    else:
        memo_section = ""
        requirements = """\
- 【メモ】がないため、天気・予定・ニュースを中心にまとめる
- 天気や予定は短く触れる程度でよい
- 【本日のニュース】がある場合は「本日のニュースピックアップ」セクションを設ける
  - 形式: <p><b>本日のニュースピックアップ</b></p><ul><li><a href="URL">タイトル</a> — 概要</li>...</ul>
  - ニュースは事実のみ掲載し、個人的な意見や感想は加えない
- カレンダーの予定に含まれる著名な会社名・組織名・個人名は、そのまま記載せず「ある会社」「ある団体」「知人」などの曖昧な表現に置き換える"""

    prompt = f"""\
以下は{today_str}の情報です。これをブログ記事としてまとめてください。

【用語の解釈】
- コスパ → スポーツクラブでの運動
- ブラサカ → ブラインドサッカー（子どもの習い事）
- みやこきっず → 合唱（子どもの習い事）
- まーちゃん → 妻

【要件】
{requirements}
- ポジティブな出来事は少しだけ前向きに表現してよいが、大げさにしない
- タイトルは 20 字以内で簡潔に
- 本文の末尾に「本日のよかったこと」セクションを必ず追加する
  - ポジティブな要素を 3 つ選び、なければ小さなことでも前向きに解釈して補う
  - 形式: <p><b>本日のよかったこと</b></p><ul><li>...</li><li>...</li><li>...</li></ul>
- マークダウン不可
- 使用する文字は JIS X 0208 の範囲に限る。en ダッシュ（–）・em ダッシュ（—）・カーリークォート（' ' " "）・黒丸（•）などの欧文特殊記号は使用しない
- 必ず以下の JSON 形式のみで返す（コードブロック不要）:
{{"title": "記事タイトル", "body": "<h3>...</h3><p>...</p>...<p><b>本日のよかったこと</b></p><ul><li>...</li></ul>"}}
{memo_section}
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
            f"{model}:generateContent"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
            method="POST"
        )
        print(f"  Gemini モデル: {model}")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    data = json.loads(res.read())
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode()
                if e.code == 429:
                    # 日次クォータ超過: リトライしても解決しないので即フォールバック
                    print(f"  Gemini 429 クォータ超過。次のモデルへフォールバック...")
                    last_error = RuntimeError(f"Gemini API エラー {e.code}: {err_body}")
                    break
                if e.code == 503 and attempt < 4:
                    wait = 30 * (attempt + 1)  # 30, 60, 90, 120 秒
                    print(f"  Gemini 503 一時エラー。{wait}秒待機後リトライ ({attempt+1}/4)...")
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


def notify_telegram_error(message: str) -> None:
    """エラー発生時に Telegram へ失敗通知を送る。"""
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    f"⚠️ ブログ自動投稿 失敗\n\n{message}",
    }).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            json.loads(res.read())
    except Exception as e:
        print(f"  [WARN] Telegram エラー通知失敗: {e}")


def normalize_for_legacy_encoding(text: str) -> str:
    """EUC-JP / Shift-JIS で表現できない Unicode 文字を近似文字に置換する。
    Gemini が生成するテキストに含まれる欧文記号をブログ投稿前に正規化する。
    """
    replacements = {
        '\u2013': 'ー',   # en dash → 長音符
        '\u2014': '―',   # em dash → 水平線（JIS X 0208 に存在）
        '\u2015': '―',   # horizontal bar（念のため）
        '\u2018': "'",   # left single quotation mark
        '\u2019': "'",   # right single quotation mark
        '\u201c': '"',   # left double quotation mark
        '\u201d': '"',   # right double quotation mark
        '\u2022': '・',  # bullet → 中点
        '\u00b7': '・',  # middle dot → 中点
        '\u00d7': '×',  # multiplication sign（JIS X 0208 に存在）
        '\u00f7': '÷',  # division sign（JIS X 0208 に存在）
        '\u00a0': ' ',   # non-breaking space → 通常スペース
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text


JUGEM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36")


def jugem_login() -> tuple:
    """JUGEM にログインし (opener, manage_base_url) を返す。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # CSRF トークン取得
    req = urllib.request.Request(
        "https://jugem.jp/login", headers={"User-Agent": JUGEM_UA})
    with opener.open(req, timeout=20) as res:
        login_html = res.read().decode("utf-8", errors="ignore")
    token_m = (
        re.search(r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']', login_html)
        or re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']', login_html)
    )
    csrf_token = token_m.group(1) if token_m else ""
    if not csrf_token:
        for c in jar:
            if c.name == "XSRF-TOKEN":
                csrf_token = urllib.parse.unquote(c.value)
                break
    # ログイン POST
    data = urllib.parse.urlencode({
        "_token": csrf_token, "account_name": JUGEM_USER,
        "password": JUGEM_PASS, "is_sub_user": "0",
        "redirect_url": "", "isSavePass": "0",
    }).encode()
    req = urllib.request.Request(
        "https://jugem.jp/login", data=data,
        headers={"User-Agent": JUGEM_UA,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://jugem.jp/login"},
        method="POST"
    )
    with opener.open(req, timeout=20) as res:
        manage_url = res.geturl()
    if "jugem.jp/login" in manage_url:
        raise RuntimeError(f"JUGEM ログイン失敗（ログインページに留まった）: {manage_url}")
    manage_base = manage_url.split("?")[0]
    return opener, manage_base


def _decode_jugem_response(raw: bytes, content_type: str) -> str:
    """JUGEM レスポンスを Content-Type に応じてデコードする。"""
    ct = content_type.lower()
    if "shift_jis" in ct or "shift-jis" in ct:
        return raw.decode("shift_jis", errors="replace")
    if "euc" in ct:
        return raw.decode("euc-jp", errors="replace")
    return raw.decode("utf-8", errors="ignore")


def check_jugem_already_posted(target_date) -> bool:
    """JUGEM に対象日の投稿が既に存在するか確認する。
    RSS フィード（XML パース）と管理画面記事一覧（テキスト照合）の両方をチェック。
    どちらかが True なら投稿済みとみなす。
    """
    try:
        opener, manage_base = jugem_login()
    except Exception as e:
        print(f"  [WARN] 既投稿チェック: ログイン失敗（スキップして続行）: {e}")
        return False

    # ── 方法1: RSS フィードで確認 ──
    try:
        blog_base = manage_base.rstrip("/").rsplit("/manage", 1)[0]
        rss_url = f"{blog_base}/?mode=rss"
        req = urllib.request.Request(rss_url, headers={"User-Agent": JUGEM_UA})
        with opener.open(req, timeout=10) as res:
            raw = res.read()
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        print(f"  （RSS: {len(items)} 件取得）")
        for item in items:
            pub_date_str = (item.findtext("pubDate") or "").strip()
            if not pub_date_str:
                continue
            dt = None
            for fmt in [
                "%a, %d %b %Y %H:%M:%S %z",   # RFC 822: "Thu, 17 Apr 2026 23:00:00 +0900"
                "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
                "%Y-%m-%d %H:%M:%S",           # "2026-04-17 23:00:00"
                "%Y-%m-%d",                    # "2026-04-17"
            ]:
                try:
                    dt = datetime.strptime(pub_date_str, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                print(f"  [WARN] RSS 日付パース失敗（未知のフォーマット）: {pub_date_str!r}")
                continue
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=JST)
            post_date = dt.astimezone(JST).date()
            if DEBUG:
                print(f"  [DEBUG] RSS 記事日付: {post_date}")
            if post_date == target_date:
                print(f"  ✅ {target_date} の投稿が既に存在します（RSS）。スキップします。")
                return True
    except Exception as e:
        print(f"  [WARN] RSS チェック失敗: {e}")

    # ── 方法2: 管理画面記事一覧で確認（RSS が使えない場合の保険）──
    try:
        req = urllib.request.Request(
            f"{manage_base}?mode=entry",
            headers={"User-Agent": JUGEM_UA}
        )
        with opener.open(req, timeout=20) as res:
            list_html = _decode_jugem_response(res.read(), res.headers.get("Content-Type", ""))
        # JUGEM の管理画面で使われる可能性のある日付フォーマットを全て試す
        date_patterns = [
            target_date.strftime("%Y.%m.%d"),      # 2026.04.17
            target_date.strftime("%Y/%m/%d"),      # 2026/04/17
            target_date.strftime("%Y-%m-%d"),      # 2026-04-17
            target_date.strftime("%Y年%m月%d日"),   # 2026年04月17日（ゼロ埋め）
            target_date.strftime("%Y年%-m月%-d日"), # 2026年4月17日（ゼロなし）
            target_date.strftime("%-m月%-d日"),    # 4月17日
        ]
        for pattern in date_patterns:
            if pattern in list_html:
                print(f"  ✅ {target_date} の投稿が既に存在します（管理画面）。スキップします。")
                return True
        if DEBUG:
            print(f"  [DEBUG] 管理画面チェック: 対象パターン未検出 ({', '.join(date_patterns)})")
    except Exception as e:
        print(f"  [WARN] 管理画面チェック失敗: {e}")

    print(f"  （{target_date} の投稿なし。投稿を続行します。）")
    return False


def post_to_jugem(title: str, body: str) -> str:
    """JUGEM ブログ管理画面フォームで記事を投稿する。"""
    opener, manage_base = jugem_login()
    print(f"  → ログイン成功: {manage_base}")

    def do_get(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": JUGEM_UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        })
        with opener.open(req, timeout=20) as res:
            raw = res.read()
            ct = res.headers.get("Content-Type", "")
            text = _decode_jugem_response(raw, ct)
            if DEBUG:
                print(f"  [DEBUG] GET {url}")
                print(f"  [DEBUG]   → final: {res.geturl()}")
                print(f"  [DEBUG]   → Content-Type: {ct}")
            return text, res.geturl()

    def do_post(url, params, encoding="utf-8", extra_headers=None):
        # EUC-JP / Shift-JIS の場合、表現できない Unicode 文字を正規化してからエンコード
        if encoding in ("euc-jp", "euc_jp", "shift_jis", "shift-jis"):
            params = {k: normalize_for_legacy_encoding(v) if isinstance(v, str) else v
                      for k, v in params.items()}
        data = urllib.parse.urlencode(params, encoding=encoding).encode('ascii')
        h = {
            "User-Agent": JUGEM_UA,
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
            text = _decode_jugem_response(raw, ct)
            if DEBUG:
                print(f"  [DEBUG]   → final: {res.geturl()}")
                print(f"  [DEBUG]   → Content-Type: {ct}")
                print(f"  [DEBUG]   → body[:500]: {text[:500]!r}")
            return text, res.geturl()

    # ── 記事投稿フォームを取得（rich view: csrf_token が含まれる）──
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
    """ブログ記事の内容を Telegram に通知する（HTML タグを平文変換）。"""
    text = body
    text = re.sub(r'<li[^>]*>', '・', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<h\d[^>]*>', '\n【', text, flags=re.IGNORECASE)
    text = re.sub(r'</h\d>', '】\n', text, flags=re.IGNORECASE)
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
    gcal_events: list[str] = []
    messages:    list[str] = []

    if test_mode:
        print("🔧 TEST_MODE: Telegram/Gemini をスキップして固定文字列で投稿します。")
        title = "テスト投稿"
        body  = "<p>これは接続確認用のテスト投稿です。自動投稿スクリプトから送信されました。</p>"
    else:
        # 1. Telegram からメモ取得
        print("📨 Telegram からメッセージを取得中...")
        messages, url_summaries, location_names, movie_infos, target_date = get_today_messages()

        # 既に今日の投稿が完了していればスキップ（30分おきの重複実行対策）
        skip_dup = os.environ.get("SKIP_DUPLICATE_CHECK", "").lower() in ("1", "true", "yes")
        if skip_dup:
            print("\n⚠️  SKIP_DUPLICATE_CHECK が有効なため、既投稿チェックをスキップします。")
        else:
            print("\n🔍 JUGEM 既投稿チェック中...")
            if check_jugem_already_posted(target_date):
                return

        if messages:
            print(f"✅  {len(messages)} 件取得:")
            for i, m in enumerate(messages, 1):
                print(f"   {i}. {m[:60]}{'...' if len(m) > 60 else ''}")
            if url_summaries:
                print(f"   URL: {len(url_summaries)} 件取得")
            if location_names:
                print(f"   位置情報: {len(location_names)} 件取得")
            if movie_infos:
                print(f"   映画: {len(movie_infos)} 件取得")
        else:
            print("📰 今日のメモなし。ニュースピックアップ記事を生成します。")

        # 2. 補足情報を収集
        print("\n🌤️  補足情報を収集中...")
        weather = get_weather(WEATHER_LOCATION)
        if weather:
            print(f"   天気: {weather}")
        github_activity = get_github_activity(GITHUB_USERNAME, GH_API_TOKEN)
        if github_activity:
            print(f"   GitHub: {len(github_activity)} 件")
        gcal_events = get_gcal_events(GCAL_CLIENT_ID, GCAL_CLIENT_SECRET, GCAL_REFRESH_TOKEN, target_date)
        if gcal_events:
            print(f"   カレンダー: {len(gcal_events)} 件")

        # メモなしの場合はニュースを取得
        news_headlines: list[dict] = []
        if not messages:
            print("📰 Yahoo!ニュース取得中...")
            news_headlines = get_news_headlines(5)
            print(f"   ニュース: {len(news_headlines)} 件取得")

        # 3. Gemini で整形
        print("\n🤖 Gemini で記事を生成中...")
        article = format_with_gemini(
            messages, weather, github_activity, gcal_events,
            url_summaries, location_names, movie_infos, news_headlines,
            target_date=target_date,
        )
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
    try:
        main()
    except Exception as e:
        msg = str(e)
        print(f"\n❌ 致命的エラー: {msg}")
        notify_telegram_error(msg[:300])
        raise
