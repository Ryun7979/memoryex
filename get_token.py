"""
Google Calendar OAuth2 リフレッシュトークン取得スクリプト（一度だけ実行）
"""
import urllib.parse
import urllib.request
import json
import webbrowser

CLIENT_ID     = input("クライアントID を貼り付けてください: ").strip()
CLIENT_SECRET = input("クライアントシークレット を貼り付けてください: ").strip()
REDIRECT_URI  = "urn:ietf:wg:oauth:2.0:oob"
SCOPE         = "https://www.googleapis.com/auth/calendar.readonly"

# 1. 認証URLを生成してブラウザで開く
auth_url = (
    "https://accounts.google.com/o/oauth2/auth"
    f"?client_id={urllib.parse.quote(CLIENT_ID)}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&scope={urllib.parse.quote(SCOPE)}"
    "&response_type=code"
    "&access_type=offline"
    "&prompt=consent"
)
print("\nブラウザが開きます。Googleアカウントでログインして認証してください。")
webbrowser.open(auth_url)

# 2. 認証コードを入力
code = input("\n認証後に表示されたコードを貼り付けてください: ").strip()

# 3. トークンを取得
data = urllib.parse.urlencode({
    "code": code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=data,
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)

try:
    with urllib.request.urlopen(req) as res:
        token = json.loads(res.read())
    print("\n=== 取得成功 ===")
    print(f"リフレッシュトークン: {token['refresh_token']}")
    print("\n↑ このトークンを GitHub Secrets の GCAL_REFRESH_TOKEN に登録してください。")
except urllib.error.HTTPError as e:
    print(f"\nエラー: {e.code} {e.reason}")
    print(e.read().decode())
