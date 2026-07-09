"""Test Bedrock-compatible API model availability."""

import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from data.ai_scraper import _create_curl_cffi_llm_session

BEDROCK_MODELS = [
    {"id": "nvidia/nemotron-3-super-120b-a12b", "name": "Nemotron-3-Super-120B", "provider": "Nvidia"},
    {"id": "meta/llama-3.3-70b-instruct", "name": "Llama-3.3-70B", "provider": "Meta"},
]

BEDROCK_API_KEY = os.getenv('BEDROCK_API_KEY')

if not BEDROCK_API_KEY:
    print("BEDROCK_API_KEY not found in .env")
    sys.exit(1)

# Decode the key to understand its format
import base64
try:
    decoded = base64.b64decode(BEDROCK_API_KEY).decode('utf-8', errors='ignore')
    print(f"Decoded key: {decoded[:50]}...")
except:
    pass

print("=" * 70)
print("BEDROCK MODEL VALIDATION")
print("=" * 70)

# Possible endpoints based on key format
ENDPOINTS = [
    "https://inference.bedrock.ai/v1/chat/completions",
    "https://api.bedrockapi.com/v1/chat/completions",
    "https://bedrock-api.io/v1/chat/completions",
    "https://api.z.ai/v1/chat/completions",
]

MODELS = BEDROCK_MODELS
session, _ = _create_curl_cffi_llm_session()

results = []

# First, find working endpoint
print("\nTesting endpoints...")
working_endpoint = None
for url in ENDPOINTS:
    try:
        resp = session.post(
            url,
            headers={
                "Authorization": f"Bearer {BEDROCK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODELS[0]["id"],
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 5,
            },
            timeout=15,
        )
        if resp.status_code != 404:
            print(f"  {url} -> HTTP {resp.status_code}")
            if resp.status_code == 200:
                working_endpoint = url
                break
    except Exception as e:
        print(f"  {url} -> {type(e).__name__}")

if not working_endpoint:
    print("\nNo working endpoint found. The API key may require:")
    print("1. A specific endpoint (check your provider's documentation)")
    print("2. AWS credentials (if using native Amazon Bedrock)")
    print("3. AWS boto3 SDK (not Bearer token auth)")
    sys.exit(1)

print(f"\nUsing endpoint: {working_endpoint}")
print()

# Test all models
for model in MODELS:
    model_id = model["id"]
    name = model["name"]
    
    print(f"[{len(results)+1}/{len(MODELS)}] {name}...", end=" ", flush=True)
    
    try:
        start = time.time()
        resp = session.post(
            working_endpoint,
            headers={
                "Authorization": f"Bearer {BEDROCK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 5,
            },
            timeout=30,
        )
        latency = time.time() - start
        
        if resp.status_code == 200:
            print(f"OK ({latency:.2f}s)")
            results.append({"model": name, "status": "OK", "latency": f"{latency:.2f}s"})
        else:
            print(f"FAIL (HTTP {resp.status_code})")
            results.append({"model": name, "status": "FAIL", "latency": "-"})
    except Exception as e:
        print(f"ERROR ({type(e).__name__})")
        results.append({"model": name, "status": "ERROR", "latency": "-"})

print()
print("=" * 70)
ok = sum(1 for r in results if r["status"] == "OK")
print(f"Results: {ok}/{len(MODELS)} models working")
print()
for r in results:
    icon = "[OK]" if r["status"] == "OK" else "[--]"
    print(f"  {icon} {r['model']:<25} {r['latency']}")
print("=" * 70)

if ok == len(MODELS):
    print("ALL MODELS OPERATIONAL")
elif ok > 0:
    print(f"{ok} MODELS WORKING")
else:
    print("ALL MODELS FAILED")
