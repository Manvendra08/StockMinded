import os
import sys
import requests

KILO_API_KEY = os.getenv("KILO_API_KEY") or os.getenv("KILO_TOKEN") or ""
URL = "https://api.kilo.ai/api/gateway/chat/completions"

models_to_test = [
    "kilo-auto/free",
    "stepfun/step-3.7-flash:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
]

print(f"Testing Kilo Gateway API at {URL}")
print(f"API Key present: {bool(KILO_API_KEY)} (Length: {len(KILO_API_KEY)})")
print("-" * 60)

headers = {
    "Authorization": f"Bearer {KILO_API_KEY}",
    "Content-Type": "application/json",
}

for m in models_to_test:
    payload = {
        "model": m,
        "messages": [
            {"role": "system", "content": "You are a professional financial assistant."},
            {"role": "user", "content": 'Respond strictly with JSON: {"status": "ok", "message": "hello"}'}
        ],
        "response_format": {"type": "json_object"}
    }
    try:
        r = requests.post(URL, json=payload, headers=headers, timeout=12)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            print(f"[OK] [{m}] HTTP 200 SUCCESS -> {content[:100]}")
        else:
            print(f"[FAIL] [{m}] HTTP {r.status_code} -> {r.text[:200]}")
    except Exception as e:
        print(f"[ERR] [{m}] ERROR -> {e}")
