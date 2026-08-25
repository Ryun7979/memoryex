"""記事本文にデータ項目が反映されているかを機械的に照合する。

Gemini には「各項目に対応する本文の抜粋」を返させ、その抜粋が本文に
実在するかを部分文字列で照合する。LLM に反映の可否を判定させない。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import re

# 抜粋がこれより短いと偶然一致してしまうため未反映として扱う
# post.py のプロンプトでは Gemini に「連続する15文字以上」の抜粋を要求しているが、
# 検証側はあえて8文字以上と緩めに通す。これは非対称に意図的なもので、モデルには
# 高い基準を課しつつ、正当な抜粋が微妙な差異で未反映と誤判定され無駄な再生成が
# 走るのを防ぐための余裕を検証側に持たせている。
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
