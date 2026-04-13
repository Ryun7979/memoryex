# check_model.py
import urllib.request, json, os

API_KEY = input("GEMINI_API_KEY を入力: ").strip()

for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}?key={API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            print(f"OK  : {model}")
    except urllib.error.HTTPError as e:
        print(f"NG ({e.code}): {model}")
