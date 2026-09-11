#!/usr/bin/env python3
"""Remove what bench/stage_demo.py created for the README screenshots: the
demo pool and its two deployments. Pools and deployments the user made are
left alone."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

client.utf8_output()
demo_models = {"Qwen/Qwen3-0.6B-GGUF", "Qwen/Qwen3-1.7B-GGUF"}
for pool in client.call("GET", "/pools"):
    if pool["name"] in ("math-team", "fast-and-smart"):
        client.call("DELETE", f"/pools/{pool['id']}")
        print("pool removed:", pool["name"])
for d in client.call("GET", "/deploys"):
    if d["spec"]["model"] in demo_models:
        client.destroy(d["id"])
        print("deploy removed:", d["spec"]["model"])
