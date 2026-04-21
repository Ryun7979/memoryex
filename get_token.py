"""
Google Calendar OAuth2 リフレッシュトークン取得スクリプト（一度だけ実行）
"""
import urllib.parse
import urllib.request
import urllib.error
import json
import webbrowser
import http.server

CLIENT_ID     = input("クライアントID を貼り付けてください: ").strip()
CLIENT_SECRET = input("クライアントシークレット を貼り付けてください: ").strip()
REDIRECT_URI  = "http://localhost:8080"
SCOPE         = "https://www.googleapis.com/auth/calendar.readonly"

# 1. 認証URLを生成
auth_url = (
    "https://accounts.google.com/o/oauth2/auth"
    f"?client_id={urllib.parse.quote(CLIENT_ID)}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&scope={urllib.parse.quote(SCOPE)}"
    "&response_type=code"
    "&access_type=offline"
    "&prompt=consent"
)

print("\n======================================================")
print("以下の URL を、nadaryu@gmail.com でログインしている")
print("ブラウザのアドレスバーに貼り付けて開いてください：")
print()
print(auth_url)
print("======================================================\n")
print("（自動でブラウザを開きます。アカウントが違う場合は")
print(" 上の URL を手動でコピーして正しいアカウントで開いてください）\n")
webbrowser.open(auth_url)

# 2. ローカルサーバーで認証コードを自動受け取り
print("認証完了を待機中... （ブラウザで認証後、自動で続行します）")

auth_code = None

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            body = "<html><body><p>認証成功！このタブを閉じてターミナルに戻ってください。</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # サーバーログを抑制

server = http.server.HTTPServer(("localhost", 8080), _Handler)
server.handle_request()

if not auth_code:
    print("\nエラー: 認証コードを取得できませんでした。")
    exit(1)

# 3. 認証コードをトークンに交換
data = urllib.parse.urlencode({
    "code":          auth_code,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri":  REDIRECT_URI,
    "grant_type":    "authorization_code",
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
