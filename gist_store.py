"""Secret Gist を JSON 状態ファイルの保存先として読み書きする。
外部ライブラリ不要（標準ライブラリのみ）。
"""

import json
import urllib.request

GIST_API = "https://api.github.com/gists"
DEFAULT_FILENAME = "memoryex-state.json"


def empty_state() -> dict:
    """初期状態を返す。呼び出しごとに新しいリストを作る。"""
    return {"offset": 0, "messages": []}


def normalize_state(raw) -> dict:
    """欠けたキーを補い、想定した型に揃える。"""
    if not isinstance(raw, dict):
        return empty_state()
    offset = raw.get("offset")
    messages = raw.get("messages")
    return {
        "offset": offset if isinstance(offset, int) and offset > 0 else 0,
        "messages": messages if isinstance(messages, list) else [],
    }


def _request(url: str, token: str, method: str = "GET", payload: bytes = None) -> dict:
    """GitHub Gist API を呼び出して JSON を返す。"""
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "memoryex",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


def load_state(gist_id: str, token: str, filename: str = DEFAULT_FILENAME) -> dict:
    """Gist から状態を読み出す。ファイルが無い・空の場合は初期状態を返す。"""
    data = _request(f"{GIST_API}/{gist_id}", token)
    entry = (data.get("files") or {}).get(filename)
    if not entry:
        return empty_state()
    if entry.get("truncated"):
        raise RuntimeError(
            f"Gist の {filename} が大きすぎて切り詰められました。保持件数を減らしてください"
        )
    content = (entry.get("content") or "").strip()
    if not content:
        return empty_state()
    try:
        return normalize_state(json.loads(content))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gist の {filename} が JSON として不正です: {e}")


def save_state(gist_id: str, token: str, state: dict, filename: str = DEFAULT_FILENAME) -> None:
    """Gist に状態を書き戻す。"""
    body = json.dumps(
        {"files": {filename: {"content": json.dumps(state, ensure_ascii=False, indent=1)}}}
    ).encode()
    _request(f"{GIST_API}/{gist_id}", token, method="PATCH", payload=body)
