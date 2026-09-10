#!/usr/bin/env python3
"""Is a llama-server deploy actually on the GPU?

Deploy the 0.6B, generate once, and compare what nvidia-smi and the container
report. A model on the GPU shows up in nvidia-smi and barely touches host RAM;
a model on the CPU does the opposite.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client  # noqa: E402

client.utf8_output()


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


with client.Deployment("Qwen/Qwen3-0.6B-GGUF", max_tokens=512, max_model_len=2048) as dep:
    started = time.time()
    text, info = dep.ask([{"role": "user", "content": "Count from 1 to 60, comma separated."}],
                         max_tokens=400)
    secs = time.time() - started
    toks = info.get("completion_tokens", 0)
    print(f"generated {toks} tokens in {secs:.1f}s -> {toks / max(secs, 0.01):.0f} tok/s")
    print("nvidia-smi processes:")
    print("  " + (sh("nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader")
                  or "(none — nothing on the GPU)"))
    print("GPU memory used:", sh("nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"))
    print("container:", sh(f"docker stats --no-stream --format '{{{{.Name}}}} {{{{.MemUsage}}}}' "
                           f"deploy-{dep.id[:12]}"))
    print("offload lines in log:")
    print("  " + (sh(f"docker logs deploy-{dep.id[:12]} 2>&1 | grep -iE 'offload|CUDA|no usable GPU|ngl' | head -5")
                  or "(none)"))
