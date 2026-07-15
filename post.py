"""
Telegram の今日のメモを Gemini で整形して JUGEM ブログに投稿するスクリプト。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import os
import re
import html
import json
import time
import http.cookiejar
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import email.utils
from datetime import datetime, timezone, timedelta

# ── 設定 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
JUGEM_USER       = os.environ["JUGEM_USER"]
JUGEM_PASS       = os.environ["JUGEM_PASS"]

JST              = timezone(timedelta(hours=9))
BLOG_BASE_URL    = "https://nadaryu.jugem.cc"
GEMINI_MODELS    = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
DEBUG            = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")

WEATHER_LOCATION   = os.environ.get("WEATHER_LOCATION", "")
GITHUB_USERNAME    = os.environ.get("GITHUB_USERNAME", "")
GH_API_TOKEN       = os.environ.get("GH_API_TOKEN", "")
GCAL_CLIENT_ID        = os.environ.get("GCAL_CLIENT_ID", "")
GCAL_CLIENT_SECRET    = os.environ.get("GCAL_CLIENT_SECRET", "")
GCAL_REFRESH_TOKEN    = os.environ.get("GCAL_REFRESH_TOKEN", "")
TMDB_API_KEY          = os.environ.get("TMDB_API_KEY", "")
# Google Fit（Fitbit Air）健康データ用 — クライアントID/SECRETはGCALと同じでも可
GHEALTH_CLIENT_ID     = os.environ.get("GHEALTH_CLIENT_ID", GCAL_CLIENT_ID)
GHEALTH_CLIENT_SECRET = os.environ.get("GHEALTH_CLIENT_SECRET", GCAL_CLIENT_SECRET)
GHEALTH_REFRESH_TOKEN = os.environ.get("GHEALTH_REFRESH_TOKEN", "")
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


def _format_http_error(e: Exception) -> str:
    """HTTPError の場合はレスポンスボディ（APIのエラー詳細）も含めて文字列化する。"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
            return f"{e.code} {e.reason}: {body}"
        except Exception:
            return f"{e.code} {e.reason}"
    return str(e)


def _civil_datetime(d, hours=0, minutes=0, seconds=0) -> dict:
    """Google Health API の CivilDateTime 形式を生成する。"""
    return {
        "date": {"year": d.year, "month": d.month, "day": d.day},
        "time": {"hours": hours, "minutes": minutes, "seconds": seconds, "nanos": 0},
    }


def _first_number(obj):
    """ネストした値オブジェクトから最初の数値を取り出す（型・キー名の揺れに対応）。"""
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, str):
        try:
            return float(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        for v in obj.values():
            n = _first_number(v)
            if n is not None:
                return n
    return None


def _health_rollup(access_token: str, data_type: str, start_date, end_date) -> list[dict]:
    """dailyRollUp で日別集計を取得し、rollup ポイントのリストを返す。"""
    payload = json.dumps({
        "range": {
            "start": _civil_datetime(start_date),
            "end":   _civil_datetime(end_date, 23, 59, 59),
        },
        "windowSizeDays": 1,
    }).encode()
    req = urllib.request.Request(
        f"https://health.googleapis.com/v4/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp",
        data=payload,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.loads(res.read())
    if DEBUG:
        print(f"  [DEBUG] {data_type} レスポンス: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data.get("rollupDataPoints") or data.get("dataPoints") or []


def _rollup_point_date(pt: dict) -> str:
    """rollup ポイントの日付を YYYY-MM-DD 形式で返す。"""
    d = pt.get("civilStartTime", {}).get("date", {})
    if d:
        return f"{d.get('year', 0):04d}-{d.get('month', 0):02d}-{d.get('day', 0):02d}"
    return ""


def _rollup_point_value(pt: dict):
    """rollup ポイントからデータ値（数値）を取り出す。"""
    for k, v in pt.items():
        if k in ("civilStartTime", "civilEndTime"):
            continue
        n = _first_number(v)
        if n is not None:
            return n
    return None


def get_health_data(client_id: str, client_secret: str, refresh_token: str, target_date=None) -> dict:
    """Google Health API v4 から健康データ（歩数・睡眠時間）を取得する。
    Fitbit Air のデータは Google Health アプリ経由で同期される。
    """
    if not all([client_id, client_secret, refresh_token]):
        return {}

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
        print(f"  [WARN] Google Health アクセストークン取得失敗: {_format_http_error(e)}")
        return {}

    if target_date is None:
        target_date = datetime.now(JST).date()

    auth_header = {"Authorization": f"Bearer {access_token}"}
    result: dict = {}

    # ── 歩数（過去7日分をまとめて取得し、当日値と週平均を算出）──
    try:
        points = _health_rollup(access_token, "steps", target_date - timedelta(days=7), target_date)
        today_key = target_date.isoformat()
        prev_values: list[float] = []
        for pt in points:
            val = _rollup_point_value(pt)
            if val is None or val <= 0:
                continue
            if _rollup_point_date(pt) == today_key:
                result["steps"] = int(val)
            else:
                prev_values.append(val)
        if prev_values:
            result["steps_week_avg"] = int(sum(prev_values) / len(prev_values))
        if DEBUG and "steps" in result:
            avg_note = f"（直近7日平均 {result['steps_week_avg']:,} 歩）" if "steps_week_avg" in result else ""
            print(f"  [DEBUG] Google Health 歩数: {result['steps']:,} 歩{avg_note}")
    except Exception as e:
        print(f"  [WARN] Google Health 歩数取得失敗: {_format_http_error(e)}")

    # ── 移動距離・消費カロリー・アクティブ時間（当日分）──
    rollup_targets = (
        ("distance",       "distance_km"),
        ("total-calories", "calories"),
        ("active-minutes", "active_minutes"),
    )
    for data_type, key in rollup_targets:
        try:
            points = _health_rollup(access_token, data_type, target_date, target_date)
            total = 0.0
            for pt in points:
                v = _rollup_point_value(pt)
                if v is not None:
                    total += v
            if total <= 0:
                continue
            if key == "distance_km":
                # 単位の揺れ対策: 20万超ならミリメートル、それ以外はメートルとみなす
                result[key] = round(total / 1_000_000, 1) if total > 200_000 else round(total / 1_000, 1)
            else:
                result[key] = int(total)
        except Exception as e:
            print(f"  [WARN] Google Health {data_type} 取得失敗: {_format_http_error(e)}")

    # ── 運動セッション（Fitbit が自動検出したウォーキング等）──
    ex_next = target_date + timedelta(days=1)
    ex_filter = (
        f'exercise.interval.civil_end_time >= "{target_date.isoformat()}"'
        f' AND exercise.interval.civil_end_time < "{ex_next.isoformat()}"'
    )
    try:
        req = urllib.request.Request(
            "https://health.googleapis.com/v4/users/me/dataTypes/exercise/dataPoints?"
            + urllib.parse.urlencode({"filter": ex_filter}),
            headers=auth_header,
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
        if DEBUG:
            print(f"  [DEBUG] 運動レスポンス: {json.dumps(data, ensure_ascii=False)[:300]}")
        exercises: list[str] = []
        for pt in data.get("dataPoints", []):
            ex = pt.get("exercise", {})
            ex_type = str(ex.get("exerciseType") or ex.get("activityType") or "運動")
            ex_type = ex_type.replace("_", " ").title()
            mins = None
            try:
                interval = ex.get("interval", {})
                s = datetime.fromisoformat(interval["startTime"].replace("Z", "+00:00"))
                e2 = datetime.fromisoformat(interval["endTime"].replace("Z", "+00:00"))
                mins = int((e2 - s).total_seconds() / 60)
            except Exception:
                pass
            exercises.append(f"{ex_type}（{mins}分）" if mins else ex_type)
        if exercises:
            result["exercises"] = exercises
    except Exception as e:
        print(f"  [WARN] Google Health 運動セッション取得失敗: {_format_http_error(e)}")

    # ── 睡眠（当日に終了したセッションを取得）──
    # civil_end_time で当日中に終わった睡眠セッションをフィルタ
    next_date = target_date + timedelta(days=1)
    sleep_filter = (
        f'sleep.interval.civil_end_time >= "{target_date.isoformat()}"'
        f' AND sleep.interval.civil_end_time < "{next_date.isoformat()}"'
    )
    sleep_params = urllib.parse.urlencode({"filter": sleep_filter})
    try:
        req = urllib.request.Request(
            f"https://health.googleapis.com/v4/users/me/dataTypes/sleep/dataPoints?{sleep_params}",
            headers=auth_header,
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
        if DEBUG:
            print(f"  [DEBUG] 睡眠レスポンス: {json.dumps(data, ensure_ascii=False)[:500]}")
        sleep_sec = 0
        for pt in data.get("dataPoints", []):
            for stage in pt.get("sleep", {}).get("stages", []):
                # AWAKE 以外（LIGHT / DEEP / REM）を睡眠時間としてカウント
                if stage.get("type", "AWAKE") != "AWAKE":
                    s = datetime.fromisoformat(stage["startTime"])
                    e = datetime.fromisoformat(stage["endTime"])
                    sleep_sec += max(0, (e - s).total_seconds())
        if sleep_sec > 0:
            result["sleep_minutes"] = int(sleep_sec / 60)
            if DEBUG:
                h, m = divmod(result["sleep_minutes"], 60)
                print(f"  [DEBUG] Google Health 睡眠: {h}時間{m}分")
    except Exception as e:
        print(f"  [WARN] Google Health 睡眠データ取得失敗: {_format_http_error(e)}")

    return result


def get_week_posts(start_date, end_date) -> list[dict]:
    """JUGEM の RSS から指定期間の記事一覧を取得する（週間ダイジェスト用）。"""
    url = f"{BLOG_BASE_URL}/?mode=rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "memoryex/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
        root = ET.fromstring(raw)
        posts: list[dict] = []
        # RSS 1.0 (RDF) / 2.0 どちらでも拾えるよう名前空間はワイルドカードで探す
        for item in root.findall(".//{*}item"):
            title = (item.findtext("{*}title") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", item.findtext("{*}description") or "")
            desc = re.sub(r"\s+", " ", desc).strip()[:200]
            pub = (item.findtext("{*}date") or item.findtext("{*}pubDate") or "").strip()
            d = None
            if pub:
                try:
                    d = datetime.fromisoformat(pub).astimezone(JST).date()
                except ValueError:
                    try:
                        d = email.utils.parsedate_to_datetime(pub).astimezone(JST).date()
                    except Exception:
                        pass
            if d is None or not (start_date <= d <= end_date):
                continue
            posts.append({"date": d.isoformat(), "title": title, "summary": desc})
        posts.sort(key=lambda p: p["date"])
        return posts
    except Exception as e:
        print(f"  [WARN] 週間記事の取得失敗: {e}")
        return []


def get_today_messages() -> tuple[list[str], list[dict], list[str]]:
    """Telegram から今日（JST）送ったメッセージとURL情報を取得する。"""
    # offset=-100: キューの「新しい方から」最大100件を取得する。
    # （デフォルトは古い順100件のため、更新が多い日に当日分が切り捨てられる）
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        "/getUpdates?limit=100&offset=-100&allowed_updates=[\"message\"]"
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

    # 診断カウンタ: メモが取得できない原因をログから特定できるようにする
    updates = data.get("result", [])
    diag_dates: dict[str, int] = {}
    diag_other_chat = 0
    diag_no_text = 0

    for update in updates:
        msg = update.get("message") or update.get("channel_post", {})
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
            diag_other_chat += 1
            continue
        ts = datetime.fromtimestamp(msg["date"], tz=JST)
        date_key = ts.date().isoformat()
        diag_dates[date_key] = diag_dates.get(date_key, 0) + 1
        if ts.date() != target_date:
            continue
        # 写真などのキャプション付きメッセージもメモとして扱う
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text:
            messages.append(text)
            for u in url_pattern.findall(text):
                if u not in found_urls:
                    found_urls.append(u)
            for m in movie_pattern.findall(text):
                t = m.strip()
                if t and t not in found_movies:
                    found_movies.append(t)
        else:
            diag_no_text += 1
        # 位置情報メッセージを収集
        loc = msg.get("location")
        if loc:
            found_locations.append({
                "lat": loc["latitude"],
                "lon": loc["longitude"],
            })

    # 診断ログ: 何件届いていて、なぜメモ0件なのかを毎回出力する
    date_summary = ", ".join(f"{d}: {n}件" for d, n in sorted(diag_dates.items())) or "なし"
    print(f"  [診断] 取得した更新: {len(updates)} 件 / 対象日: {target_date}")
    print(f"  [診断] 日付別の内訳: {date_summary}")
    if diag_other_chat:
        print(f"  [診断] 対象外チャットの更新: {diag_other_chat} 件")
    if diag_no_text:
        print(f"  [診断] テキストなし（スタンプ・位置情報等）: {diag_no_text} 件")

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


def _strip_code_fences(text: str) -> str:
    """```json ... ``` コードブロックを除去する。"""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(
            l for l in text.splitlines() if not l.startswith("```")
        ).strip()
    return text


def _call_gemini(prompt: str, thinking_budget: int = 5000) -> str:
    """Gemini API を呼び出して生成テキストを返す（モデルフォールバック付き）。"""
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,          # thinking 有効時は 1.0 が推奨
            "thinkingConfig": {
                "thinkingBudget": thinking_budget,  # thinking トークン上限（0=無効、-1=動的）
            },
        },
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

    return _strip_code_fences(data["candidates"][0]["content"]["parts"][0]["text"])


def build_data_items(
    messages: list[str],
    gcal_events: list[str],
    github_activity: list[str],
    url_summaries: list[dict],
    location_names: list[str],
    movie_infos: list[dict],
) -> list[str]:
    """記事に必ず反映すべきデータ項目の一覧を作る（反映漏れ検査用）。"""
    items: list[str] = []
    items += [f"メモ: {m}" for m in messages]
    items += [f"予定: {e}" for e in gcal_events]
    items += [f"GitHub: {a}" for a in github_activity]
    items += [f"共有URL: {s['title'] or s['url']}" for s in url_summaries]
    items += [f"訪問場所: {n}" for n in location_names]
    items += [f"映画: {m['title']}" for m in movie_infos]
    return items


def check_article_coverage(items: list[str], body: str) -> list[str]:
    """記事本文に反映されていないデータ項目を Gemini に判定させて返す。
    判定に失敗した場合は空リストを返す（投稿自体は止めない）。
    """
    if not items:
        return []
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    numbered = "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1))
    prompt = f"""\
以下の【データ項目】のそれぞれが【記事本文】に反映されているかを判定してください。

判定基準:
- 言い換え・要約されていても、その項目の内容に触れていれば「反映されている」とみなす
- 会社名・団体名・人名が「ある会社」「知人」などの曖昧な表現に置き換えられていても「反映されている」とみなす
- 項目の内容が本文のどこにも一切登場しない場合のみ「未反映」とする

【データ項目】
{numbered}

【記事本文】
{plain}

未反映の項目の番号だけを JSON 配列で返してください（例: [2, 5]）。
全て反映されていれば [] を返してください。JSON 配列のみを返すこと。"""
    try:
        raw = _call_gemini(prompt, thinking_budget=2000)
        nums = json.loads(raw)
        return [items[n - 1] for n in nums
                if isinstance(n, int) and 1 <= n <= len(items)]
    except Exception as e:
        print(f"  [WARN] 反映漏れチェック失敗（チェックをスキップ）: {e}")
        return []


def format_with_gemini(
    messages: list[str],
    weather: str = "",
    github_activity: list[str] = [],
    gcal_events: list[str] = [],
    url_summaries: list[dict] = [],
    location_names: list[str] = [],
    movie_infos: list[dict] = [],
    news_headlines: list[dict] = [],
    health_data: dict = {},
    week_posts: list[dict] = [],
    target_date=None,
    missing_feedback: list[str] = [],
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
    if health_data:
        lines = []
        steps = health_data.get("steps")
        week_avg = health_data.get("steps_week_avg")
        sleep_min = health_data.get("sleep_minutes")
        if steps is not None:
            if steps >= 8000:
                steps_comment = "よく動いた一日"
            elif steps >= 5000:
                steps_comment = "まずまずの活動量"
            elif steps > 0:
                steps_comment = "あまり動けなかった一日"
            else:
                steps_comment = ""
            line = f"・歩数: {steps:,}歩"
            if steps_comment:
                line += f"（{steps_comment}）"
            # 直近7日平均との比較コメント（±15% を同程度とみなす）
            if week_avg:
                if steps >= week_avg * 1.15:
                    line += f"。直近7日平均（{week_avg:,}歩）より多め"
                elif steps <= week_avg * 0.85:
                    line += f"。直近7日平均（{week_avg:,}歩）より少なめ"
                else:
                    line += f"。直近7日平均（{week_avg:,}歩）と同程度"
            lines.append(line)
        if sleep_min is not None:
            h, m = divmod(sleep_min, 60)
            sleep_str = f"{h}時間{m}分" if m else f"{h}時間"
            if sleep_min >= 420:
                sleep_comment = "しっかり眠れた"
            elif sleep_min >= 360:
                sleep_comment = "まあまあの睡眠"
            else:
                sleep_comment = "少し睡眠が短め"
            lines.append(f"・睡眠時間: {sleep_str}（{sleep_comment}）")
        if health_data.get("distance_km"):
            lines.append(f"・移動距離: {health_data['distance_km']}km")
        if health_data.get("calories"):
            lines.append(f"・消費カロリー: 約{health_data['calories']:,}kcal")
        if health_data.get("active_minutes"):
            lines.append(f"・アクティブ時間: {health_data['active_minutes']}分")
        if health_data.get("exercises"):
            lines.append("・検出された運動: " + "、".join(health_data["exercises"]))
        if lines:
            extra_sections += "\n【健康データ（Fitbit）】\n" + "\n".join(lines) + "\n"
    if week_posts:
        lines = [f"・{p['date']} 「{p['title']}」: {p['summary']}" for p in week_posts]
        extra_sections += "\n【今週の記事一覧】\n" + "\n".join(lines) + "\n"
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
- 【メモ】の各行は一つ残らず記事に反映する。一部だけを取り上げて他を省略しない。短いメモや些細なメモも必ず一文以上で触れる
- 【今日の予定】【GitHub 活動】【訪問場所】【鑑賞した映画】など、提供された補足情報も各項目を全て本文のどこかで必ず記述する。要約して間引いたり、項目を省略したりしない
- メモの内容を自然な日記文に仕上げる。過度な装飾・大げさな表現・接続詞の多用は避ける
- 話題が変わるごとに <p> タグで段落を分け、読みやすくする
- 大きく話題が変わる箇所のみ <h3> タグでサブタイトルをつける。細かい話題ごとにはつけない
- 補足情報（カレンダー・GitHub・訪問場所・映画・健康データ）はメモとは独立した段落として記述する。メモの話題と無理につなげない
- 天気は冒頭に一文で添える程度でよい
- 【共有URL】がある場合は、本文とは別に「本日の気になったインターネット」セクションを設ける
  - 形式: <p><b>本日の気になったインターネット</b></p><ul><li><a href="URL">タイトル</a> — 紹介文</li>...</ul>
- カレンダーの予定に含まれる著名な会社名・組織名・個人名は、そのまま記載せず「ある会社」「ある団体」「知人」などの曖昧な表現に置き換える"""
    else:
        memo_section = ""
        requirements = """\
- 【メモ】がないため、天気・予定・健康データ・ニュースを中心にまとめる
- 【今日の予定】【GitHub 活動】【訪問場所】【鑑賞した映画】など、提供された補足情報は各項目を全て本文のどこかで必ず記述する。項目を省略しない
- 天気は短く触れる程度でよい
- 【健康データ（Fitbit）】がある場合は、歩数や睡眠について本文中で必ず触れる
- 【本日のニュース】がある場合は「本日のニュースピックアップ」セクションを設ける
  - 形式: <p><b>本日のニュースピックアップ</b></p><ul><li><a href="URL">タイトル</a> — 概要</li>...</ul>
  - ニュースは事実のみ掲載し、個人的な意見や感想は加えない
- カレンダーの予定に含まれる著名な会社名・組織名・個人名は、そのまま記載せず「ある会社」「ある団体」「知人」などの曖昧な表現に置き換える"""

    feedback_section = ""
    if missing_feedback:
        fb = "\n".join(f"・{x}" for x in missing_feedback)
        feedback_section = f"""
【前回の生成で漏れていた項目】
前回の生成では以下の項目が記事に反映されていませんでした。今回は必ず全て本文に反映してください。
{fb}
"""

    weekly_req = ""
    if week_posts:
        weekly_req = """
- 今日は日曜のため、本文の末尾（「本日のよかったこと」の直前）に <h3>今週のダイジェスト</h3> セクションを設ける
  - 【今週の記事一覧】をもとに、1週間の出来事の流れが分かる文章を3〜5文でまとめる
  - 記事タイトルの羅列にせず、印象的な出来事を拾って自然な振り返りの文章にする"""

    prompt = f"""\
以下は{today_str}の情報です。これをブログ記事としてまとめてください。

【文体の参考例】
以下は理想とする文体・トーンのサンプルです。この雰囲気に合わせて書いてください。

---
今日の京都は朝から雲が多めで、気温も少し肌寒かった。

午前中は仕事の打ち合わせが立て続けにあって、気づいたらお昼を回っていた。お腹もすいたし、気分転換に以前から気になっていたタコスの店へ。見た目はかわいいんだけど、これで1,000円かあ……というのが正直なところ。おいしかったけど、もう少しボリュームがほしかった。

帰宅してからはむちゅこの宿泊学習の準備を少し手伝った。要項がYouTube動画で届くのは今どきだなと思いつつ、AIに書き出してもらったら持ち物リストが5分で完成。便利な時代になったものだ。

夜はモンハンを少しやったけど、最近なんとなく集中できていない。そろそろ潮時かもしれない。
---

【用語の解釈】（メモに以下の用語が登場した場合のみ、括弧内の意味として解釈する）
- コスパ → スポーツクラブでの運動
- ブラサカ → ブラインドサッカー（子どもの習い事）
- みやこきっず → 合唱（子どもの習い事）
- まーちゃん → 妻

【絶対に守るルール】
- メモ・補足情報に記載のない事実・行動・感情は一切追加しない
- 時間帯（午前・午後・朝・夜など）はメモに明記されている場合のみ記述する
- 上記の用語解釈はメモに該当の用語が含まれる場合にのみ使用する。メモにない話題を補うために使わない
- メモの出来事と補足情報（カレンダー・GitHub・天気など）を因果関係・時系列・感想でつなげた文を作らない
  - 悪い例：「お腹が空いたので、午後のコスパで運動しようと思います」（メモとカレンダーを無理に結合）
  - 悪い例：「天気が良かったので外出が楽しめた」（メモに記載がない場合）
- メモの内容と補足情報は、話題ごとに独立した段落として別々に記述する
- 【最重要】提供されたデータ（メモの各行・予定の各件・その他セクションの各項目）は全て記事に反映する。出力する前に、各セクションの全項目が本文に含まれているかを一つずつ確認し、漏れがあれば追記してから出力する
{feedback_section}
【要件】
{requirements}{weekly_req}
- 【健康データ（Fitbit）】がある場合は、メモの有無にかかわらず、歩数・睡眠時間に必ず本文中で触れ、一言感想を添える（例：「今日はよく歩いた」「あまり動けなかったので明日は意識したい」「しっかり眠れた」など）。大げさにせず、さらっと触れる程度でよい
- ポジティブな出来事は少しだけ前向きに表現してよいが、大げさにしない
- タイトルは 20 字以内で簡潔に
- 本文の末尾に「本日のよかったこと」セクションを必ず追加する
  - メモ・補足情報に実際に登場するポジティブな要素を 3 つ選ぶ。なければ記載された出来事を前向きに解釈して補う
  - 形式: <p><b>本日のよかったこと</b></p><ul><li>...</li><li>...</li><li>...</li></ul>
- マークダウン不可
- 使用する文字は JIS X 0208 の範囲に限る。en ダッシュ（–）・em ダッシュ（—）・カーリークォート（' ' " "）・黒丸（•）などの欧文特殊記号は使用しない
- 必ず以下の JSON 形式のみで返す（コードブロック不要）:
{{"title": "記事タイトル", "body": "<h3>...</h3><p>...</p>...<p><b>本日のよかったこと</b></p><ul><li>...</li></ul>"}}
{memo_section}
{extra_sections}"""

    raw_text = _call_gemini(prompt)

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
    print(f"=== 実行開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')} ===", flush=True)

    test_mode = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

    # ── TEST_MODE: 1回だけ固定文字列で投稿して終了 ──
    if test_mode:
        print("🔧 TEST_MODE: Telegram/Gemini をスキップして固定文字列で投稿します。")
        title = "テスト投稿"
        body  = "<p>これは接続確認用のテスト投稿です。自動投稿スクリプトから送信されました。</p>"
        post_id = post_to_jugem(title, body)
        blog_url = f"{BLOG_BASE_URL}/?eid={post_id}"
        print(f"✅  投稿完了！ post_id = {post_id}")
        print(f"   URL: {blog_url}")
        notify_telegram(title, body, blog_url)
        print("✅  Telegram 通知完了")
        return

    # ── 通常モード: 02:00 JST までリトライループ ──
    now = datetime.now(JST)
    today_2am = datetime(now.year, now.month, now.day, 2, 0, 0, tzinfo=JST)
    # 23:00 JST 起動なので締切は翌日 02:00（当日 02:00 はすでに過去）
    deadline = today_2am if now < today_2am else today_2am + timedelta(days=1)
    print(f"  投稿締切: {deadline.strftime('%Y-%m-%d %H:%M JST')}", flush=True)

    last_error: str = ""
    attempt = 0
    while True:
        now = datetime.now(JST)
        if now >= deadline:
            raise RuntimeError(
                f"02:00 JST までに投稿できませんでした（{attempt} 回試行）。"
                + (f" 最後のエラー: {last_error}" if last_error else "")
            )

        attempt += 1
        print(f"\n--- 試行 {attempt} 回目: {now.strftime('%H:%M JST')} ---", flush=True)

        try:
            # 1. Telegram からメモ取得
            print("📨 Telegram からメッセージを取得中...")
            messages, url_summaries, location_names, movie_infos, target_date = get_today_messages()

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

            # カレンダーの「映画 〇〇」予定は鑑賞した映画として扱う
            # （予定一覧からは除外し、TMDB 情報付きで【鑑賞した映画】に載せる）
            movie_event_pattern = re.compile(r"^(?:\d{1,2}:\d{2}|終日)\s+映画[　 :：、,]?\s*(.+)")
            remaining_events: list[str] = []
            for ev in gcal_events:
                m = movie_event_pattern.match(ev)
                title = m.group(1).strip() if m else ""
                if title and all(title != mi.get("title") for mi in movie_infos):
                    movie_infos.append(fetch_movie_info(title))
                    print(f"   カレンダーから映画を検出: {title}")
                else:
                    remaining_events.append(ev)
            gcal_events = remaining_events

            news_headlines: list[dict] = []
            if not messages:
                print("📰 Yahoo!ニュース取得中...")
                news_headlines = get_news_headlines(5)
                print(f"   ニュース: {len(news_headlines)} 件取得")

            # 健康データ（Google Fit / Fitbit Air）
            health_data: dict = {}
            if GHEALTH_REFRESH_TOKEN:
                print("💪 Google Fit から健康データを取得中...")
                health_data = get_health_data(
                    GHEALTH_CLIENT_ID, GHEALTH_CLIENT_SECRET,
                    GHEALTH_REFRESH_TOKEN, target_date,
                )
                if health_data:
                    steps = health_data.get("steps")
                    sleep_min = health_data.get("sleep_minutes")
                    parts = []
                    if steps is not None:
                        parts.append(f"歩数 {steps:,}歩")
                    if sleep_min is not None:
                        h, m = divmod(sleep_min, 60)
                        parts.append(f"睡眠 {h}時間{m}分" if m else f"睡眠 {h}時間")
                    print(f"   健康データ: {', '.join(parts)}")
                else:
                    print("   健康データ: 取得できませんでした（データなしまたはエラー）")

            # 日曜は週間ダイジェスト用に今週の記事一覧を収集
            week_posts: list[dict] = []
            if target_date.weekday() == 6:
                print("📚 日曜のため週間ダイジェスト用の記事を収集中...")
                week_posts = get_week_posts(target_date - timedelta(days=6), target_date)
                print(f"   今週の記事: {len(week_posts)} 件")

            # 3. Gemini で記事生成
            print("\n🤖 Gemini で記事を生成中...")
            article = format_with_gemini(
                messages, weather, github_activity, gcal_events,
                url_summaries, location_names, movie_infos, news_headlines,
                health_data=health_data,
                week_posts=week_posts,
                target_date=target_date,
            )
            title = article["title"]
            body  = article["body"]
            print(f"✅  タイトル: {title}")

            # 3.5 反映漏れチェック: 拾ったデータが全て記事に含まれているか検査し、
            #     漏れがあれば再生成（最大2回）。それでも漏れたら記事末尾に追記する。
            data_items = build_data_items(
                messages, gcal_events, github_activity,
                url_summaries, location_names, movie_infos,
            )
            missing = check_article_coverage(data_items, body)
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
                missing = check_article_coverage(data_items, body)
            if missing:
                print(f"⚠️  再生成後も {len(missing)} 件が未反映のため、記事末尾に追記します")
                lis = "".join(f"<li>{html.escape(x)}</li>" for x in missing)
                body += f"<p><b>そのほかの記録</b></p><ul>{lis}</ul>"

            # 4. JUGEM に投稿
            print("\n📝 JUGEM に投稿中...")
            post_id = post_to_jugem(title, body)
            blog_url = f"{BLOG_BASE_URL}/?eid={post_id}"
            print(f"✅  投稿完了！ post_id = {post_id}")
            print(f"   URL: {blog_url}")

            # 5. Telegram に通知
            print("\n📨 Telegram に通知中...")
            notify_telegram(title, body, blog_url)
            print("✅  Telegram 通知完了")
            return  # 成功: ループ終了

        except Exception as e:
            last_error = str(e)
            print(f"\n❌ 試行 {attempt} 失敗: {last_error}", flush=True)

            next_time = datetime.now(JST) + timedelta(minutes=30)
            if next_time >= deadline:
                raise RuntimeError(
                    f"試行 {attempt} 回失敗し、次の試行時刻 ({next_time.strftime('%H:%M JST')}) "
                    f"が締切を超えるため終了します。最後のエラー: {last_error}"
                )

            print(f"  30分後に再試行します（次回: {next_time.strftime('%H:%M JST')}）...", flush=True)
            time.sleep(30 * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = str(e)
        print(f"\n❌ 致命的エラー: {msg}")
        notify_telegram_error(msg[:300])
        raise
