"""
Standalone Groq connectivity test — isolates whether the problem is the key,
the model name, or something else, independent of judge_simulator.py.

Usage:
    python test_groq_key.py "gsk_your_key_here"
"""

import sys
import json
import urllib.request
import urllib.error

if len(sys.argv) < 2:
    print("Usage: python test_groq_key.py \"gsk_your_key_here\"")
    sys.exit(1)

api_key = sys.argv[1].strip()

print(f"Key length: {len(api_key)}")
print(f"Key starts with: {api_key[:8]!r}")
print(f"Key ends with:   {api_key[-6:]!r}")
print(f"Contains whitespace/newline: {any(c.isspace() for c in api_key)}")
print()

# First, list the models this key actually has access to.
list_req = urllib.request.Request(
    "https://api.groq.com/openai/v1/models",
    headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    },
)
try:
    with urllib.request.urlopen(list_req, timeout=20) as resp:
        models = json.loads(resp.read().decode("utf-8"))
        ids = [m["id"] for m in models.get("data", [])]
        print(f"Models available to this key ({len(ids)}):")
        for mid in ids:
            print(f"  - {mid}")
        print()
except Exception as e:
    print(f"Could not list models: {type(e).__name__}: {e}\n")
    ids = []

test_model = ids[0] if ids else "llama-3.3-70b-versatile"
print(f"Testing a chat completion with model: {test_model}\n")

req = urllib.request.Request(
    "https://api.groq.com/openai/v1/chat/completions",
    data=json.dumps({
        "model": test_model,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"SUCCESS — HTTP {resp.status}")
        print(resp.read().decode("utf-8")[:500])
except urllib.error.HTTPError as e:
    print(f"FAILED — HTTP {e.code}")
    print(e.read().decode("utf-8")[:1000])
except Exception as e:
    print(f"FAILED — {type(e).__name__}: {e}")
