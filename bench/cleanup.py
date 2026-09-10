#!/usr/bin/env python3
"""Destroy deployments left behind by an interrupted benchmark.

With 6GB of VRAM an orphaned model from a killed run is not harmless: it holds
memory the next model needs. `--all` clears every deployment (they are
disposable); without it only failed ones go.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

everything = "--all" in sys.argv
gone = 0
for d in client.call("GET", "/deploys"):
    if everything or d.get("status") == "failed":
        client.destroy(d["id"])
        gone += 1
print(f"removed {gone} deploy(s)")
