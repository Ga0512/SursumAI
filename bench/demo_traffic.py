#!/usr/bin/env python3
"""Keep a little traffic on the demo deployments so screenshots show live
metrics. Runs for a few minutes and stops on its own."""
import itertools, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

deploys = [d["id"] for d in client.call("GET", "/deploys") if d["status"] == "healthy"]
prompts = ["Explain what a GPU does in two sentences.", "Give me three names for a cat.",
           "What is 17 * 23? Show the steps.", "Summarise why local AI matters."]
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 420)
for did, q in zip(itertools.cycle(deploys), itertools.cycle(prompts)):
    if time.time() > end:
        break
    try:
        client.call("POST", f"/deploys/{did}/chat",
                    {"messages": [{"role": "user", "content": q}], "max_tokens": 300}, timeout=300)
    except client.BenchError as e:
        print("skip:", e)
