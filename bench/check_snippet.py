#!/usr/bin/env python3
"""The Details → Code snippet, run for real: OpenAI client, absolute base URL,
model addressed by deploy id and by pool name."""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

base = "http://localhost:8001/v1"
deploys = [d for d in client.call("GET", "/deploys") if d["status"] == "healthy"]
pools = client.call("GET", "/pools")
targets = [deploys[0]["id"]] + [p["name"] for p in pools if p["name"] == "math-team"]
for model in targets:
    req = urllib.request.Request(
        f"{base}/chat/completions", method="POST",
        data=json.dumps({"model": model, "max_tokens": 200,
                         "messages": [{"role": "user", "content": "Say OK."}]}).encode())
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {client.token()}")
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    print(f"model={model[:16]:<16} -> {r.status}  served by: {out.get('model')}")
