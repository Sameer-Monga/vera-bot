"""
Cross-platform smoke test for the Vera bot — pure stdlib, no curl/bash needed.
Works identically on Windows (cmd, PowerShell, Git Bash), macOS and Linux.

Usage:
    python smoke_test.py
    python smoke_test.py http://localhost:8080 ./dataset
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

BOT_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
DATASET_DIR = sys.argv[2] if len(sys.argv) > 2 else "./dataset"


def call(method: str, path: str, body: dict | None = None):
    url = BOT_URL.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(label: str, status: int, payload: dict):
    print(f"\n== {label} (HTTP {status}) ==")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def push(scope: str, context_id: str, version: int, delivered_at: str, payload: dict):
    return call("POST", "/v1/context", {
        "scope": scope,
        "context_id": context_id,
        "version": version,
        "delivered_at": delivered_at,
        "payload": payload,
    })


def main():
    status, body = call("GET", "/v1/healthz")
    show("healthz (before context)", status, body)

    status, body = call("GET", "/v1/metadata")
    show("metadata", status, body)

    category = load_json(f"{DATASET_DIR}/categories/dentists.json")
    status, body = push("category", "dentists", 1, "2026-04-26T09:45:00Z", category)
    show("push category: dentists", status, body)

    merchants = load_json(f"{DATASET_DIR}/merchants_seed.json")["merchants"]
    merchant = next(m for m in merchants if m["merchant_id"] == "m_001_drmeera_dentist_delhi")
    status, body = push("merchant", "m_001_drmeera_dentist_delhi", 1, "2026-04-26T09:45:30Z", merchant)
    show("push merchant: m_001_drmeera_dentist_delhi", status, body)

    triggers = load_json(f"{DATASET_DIR}/triggers_seed.json")["triggers"]
    trigger = next(t for t in triggers if t["id"] == "trg_001_research_digest_dentists")
    status, body = push("trigger", "trg_001_research_digest_dentists", 1, "2026-04-26T10:32:00Z", trigger)
    show("push trigger: trg_001_research_digest_dentists", status, body)

    status, body = call("GET", "/v1/healthz")
    show("healthz (after context — counts should be 1/1/1)", status, body)

    status, body = call("POST", "/v1/tick", {
        "now": "2026-04-26T10:35:00Z",
        "available_triggers": ["trg_001_research_digest_dentists"],
    })
    show("tick (bot should compose a message)", status, body)

    conv_id = "conv_m_001_drmeera_dentist_delhi_trg_001_research_digest_dentists"
    status, body = call("POST", "/v1/reply", {
        "conversation_id": conv_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Yes please send the abstract",
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    })
    show("reply: engaged merchant", status, body)

    status, body = call("POST", "/v1/reply", {
        "conversation_id": "conv_auto_1",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Thank you for contacting us! Our team will respond shortly.",
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    })
    show("reply: auto-reply pattern", status, body)

    status, body = call("POST", "/v1/reply", {
        "conversation_id": "conv_intent_1",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Ok lets do it, whats next?",
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    })
    show("reply: intent transition", status, body)

    status, body = call("POST", "/v1/reply", {
        "conversation_id": "conv_hostile",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    })
    show("reply: hostile", status, body)

    print("\nAll smoke tests sent. Review the JSON above.")


if __name__ == "__main__":
    main()
