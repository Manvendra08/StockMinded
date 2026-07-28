import os
import sys
import requests
from data.ai_scraper import _get_ai_config, call_llm

print("=== Kilo Gateway Integration Diagnostic ===")

# 1. Config Check
cfg = _get_ai_config()
print(f"ScrapeGraphAI Config Loaded: {bool(cfg)}")
if cfg:
    kilo_key = cfg.get("kilo_api_key")
    print(f"Kilo API Key configured in StockMinded: {bool(kilo_key)}")
else:
    kilo_key = None

# 2. Live Catalog Verification
print("\n--- Querying Kilo Gateway Live Catalog ---")
try:
    cat_res = requests.get("https://api.kilo.ai/api/gateway/models", timeout=10)
    if cat_res.status_code == 200:
        models = cat_res.json().get("data", [])
        free_models = [m for m in models if m.get("isFree") or "free" in m["id"].lower()]
        print(f"Catalog Status: HTTP 200 SUCCESS ({len(models)} total models, {len(free_models)} free models)")
        print("Top Free Models Catalog:")
        for fm in free_models[:10]:
            print(f"  - Model ID: {fm['id']} | Context: {fm.get('context_length')} tokens | Name: {fm.get('name')}")
    else:
        print(f"Catalog query failed: HTTP {cat_res.status_code}")
except Exception as e:
    print(f"Catalog query error: {e}")

# 3. Model Execution Test (with API Key if available)
print("\n--- Testing Model Execution ---")
test_key = kilo_key or os.getenv("KILO_API_KEY")
if not test_key:
    print("NOTE: KILO_API_KEY environment variable is not set yet.")
    print("      To execute requests against Kilo Gateway, generate your Kilo API key from https://kilo.ai")
    print("      and set it in environment (env:KILO_API_KEY='your_key') or in config.yaml under scrapegraphai: kilo_api_key.")
else:
    print(f"Testing execution with key (length: {len(test_key)})...")
    url = "https://api.kilo.ai/api/gateway/chat/completions"
    headers = {"Authorization": f"Bearer {test_key}", "Content-Type": "application/json"}
    for m in ["kilo-auto/free", "stepfun/step-3.7-flash:free", "cohere/north-mini-code:free", "openrouter/free"]:
        try:
            p = {
                "model": m,
                "messages": [
                    {"role": "system", "content": "You are a financial analysis assistant."},
                    {"role": "user", "content": 'Return JSON: {"status": "ok"}'}
                ],
                "response_format": {"type": "json_object"}
            }
            r = requests.post(url, json=p, headers=headers, timeout=10)
            if r.status_code == 200:
                print(f"[SUCCESS] {m} -> {r.json()['choices'][0]['message']['content']}")
            else:
                print(f"[RESPONSE] {m} -> HTTP {r.status_code}: {r.text[:150]}")
        except Exception as ex:
            print(f"[ERROR] {m} -> {ex}")

print("\n=== Kilo Gateway Integration Check Complete ===")
