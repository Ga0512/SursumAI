#!/usr/bin/env python3
"""Put real data on screen for the README screenshots.

Deploys two small models, groups them in a pool and sends a few messages so
the dashboard has metrics, sparklines and routed answers to show. Nothing here
is benchmark data; the deployments are disposable.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

client.utf8_output()
MODELS = ["Qwen/Qwen3-0.6B-GGUF", "Qwen/Qwen3-1.7B-GGUF"]

ids = []
for m in MODELS:
    d = client.deploy(m, max_model_len=4096, max_tokens=1024)
    print("deploying", m, d["id"])
    ids.append(d["id"])
for i in ids:
    client.wait_healthy(i)

pool = client.call("POST", "/pools", {"name": "math-team", "model_ids": ids,
                                      "mode": "escalation"})
print("pool", pool["id"])

questions = [
    ("hi! what can you help me with?", ids[0]),
    ("Janet has 16 eggs, eats 3 and bakes with 4. She sells the rest at $2. How much does she make?", ids[0]),
    ("Write a haiku about local AI.", ids[1]),
    ("A train leaves at 14:00 at 80 km/h and another at 15:00 at 120 km/h. When does the second catch up?", ids[1]),
]
for q, did in questions:
    started = time.time()
    client.call("POST", f"/deploys/{did}/chat",
                {"messages": [{"role": "user", "content": q}], "max_tokens": 600},
                timeout=600)
    print(f"  chat {did[:8]} {time.time() - started:.0f}s")
print("ready")
