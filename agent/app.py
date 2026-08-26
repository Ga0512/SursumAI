from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from core import metrics
from core.spec import Spec, SpecError
from . import executor, executor_llama

app = FastAPI(title="SursumAI Agent")

AGENT_KEY = os.environ.get("AGENT_KEY", "dev-agent-key")


def _executor(runtime: str):
    return executor_llama if runtime == "llama" else executor


class StartRequest(BaseModel):
    deploy_id: str
    spec: dict


class PreflightRequest(BaseModel):
    spec: dict


def _require_key(x_agent_key: str | None) -> None:
    if x_agent_key != AGENT_KEY:
        raise HTTPException(status_code=401, detail="invalid agent key")


def _probe_healthy(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/models", timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


@app.get("/health")
async def health():
    return {"status": "ok"}


def _gpu() -> bool:
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _docker() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _gpu_vram_mb() -> tuple[int, int] | None:
    """(total_mb, free_mb) of the first NVIDIA GPU, or None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.decode(errors="replace").splitlines()
        if not parts or "," not in parts[0]:
            return None
        total, free = parts[0].split(",")
        return int(total.strip()), int(free.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None


def _hf_sizes(model: str) -> dict[str, int] | None:
    """Map of file name -> size in bytes for a HF repo (None if unreachable)."""
    url = f"https://huggingface.co/api/models/{model}?blobs=true"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
            return {
                s.get("rfilename", ""): (s.get("size") or 0)
                for s in data.get("siblings", [])
            }
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


@app.get("/capabilities")
async def capabilities():
    """What can this machine run? Used by the UI to pre-select the runtime."""
    gpu = _gpu()
    docker = _docker()
    vram = _gpu_vram_mb()
    return {
        "gpu": gpu,
        "docker": docker,
        "vram_total_mb": vram[0] if vram else None,
        "vram_free_mb": vram[1] if vram else None,
        "recommended_runtime": "vllm" if (gpu and docker) else "llama",
    }


@app.get("/model_fit")
async def model_fit(model: str, runtime: str = "llama"):
    """Suggest gpu_memory_utilization / max_model_len so a model fits in VRAM.

    Pure suggestion: the user may override the values in the UI."""
    vram = _gpu_vram_mb()
    sizes = _hf_sizes(model)
    if vram is None or sizes is None:
        return {"ok": False, "reason": "unavailable"}
    total_mb, free_mb = vram

    if runtime == "vllm":
        weights_bytes = sum(v for name, v in sizes.items() if name.endswith(".safetensors"))
    else:
        gguf = [v for name, v in sizes.items() if name.endswith(".gguf") and not name.startswith("mmproj")]
        weights_bytes = max(gguf) if gguf else 0
    weights_mb = weights_bytes / (1024 * 1024)

    # heuristic: weights + ~15% headroom should fit in the utilization budget
    if total_mb <= 0:
        return {"ok": False, "reason": "no gpu"}
    budget = weights_mb * 1.15
    if weights_mb <= 0:
        return {"ok": False, "reason": "no weights found"}
    if budget > total_mb:
        return {
            "ok": True,
            "fits": False,
            "weights_mb": round(weights_mb),
            "vram_total_mb": total_mb,
            "vram_free_mb": free_mb,
            "message": f"weights need ~{weights_mb / 1024:.1f}GB of VRAM but this GPU has {total_mb / 1024:.1f}GB — it will not fit. Pick a smaller model or a lower-quant GGUF.",
        }
    util = round(min(max(budget / total_mb, 0.25), 0.95), 2)

    # naive KV-cache room guess: leave ~30% of free VRAM for context
    room_mb = max(free_mb - weights_mb * 0.85, 0)
    suggested_len = int(max(min(room_mb * 0.3 * (1024 / 16), 16384), 128))

    return {
        "ok": True,
        "fits": True,
        "weights_mb": round(weights_mb),
        "vram_total_mb": total_mb,
        "vram_free_mb": free_mb,
        "suggest": {"gpu_memory_utilization": util, "max_model_len": suggested_len},
    }


@app.post("/preflight")
async def preflight(req: PreflightRequest, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    try:
        spec = Spec.from_dict(req.spec)
        spec.validate()
    except SpecError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    checks = await asyncio.to_thread(_executor(spec.runtime).preflight, spec)
    return {"runtime": spec.runtime, "checks": checks}


@app.post("/deploys", status_code=202)
async def start_deploy(req: StartRequest, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    try:
        spec = Spec.from_dict(req.spec)
        spec.validate()
    except SpecError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    asyncio.create_task(_deploy_job(req.deploy_id, spec))
    return {"deploy_id": req.deploy_id, "status": "started"}


SPECS: dict[str, Spec] = {}


def _spec_for(deploy_id: str) -> Spec | None:
    return SPECS.get(deploy_id)


async def _deploy_job(deploy_id: str, spec: Spec) -> None:
    SPECS[deploy_id] = spec
    try:
        await asyncio.to_thread(_executor(spec.runtime).start, spec, deploy_id)
    except (executor.TransportError, executor_llama.TransportError):
        pass


@app.get("/deploys/{deploy_id}/status")
async def deploy_status(deploy_id: str, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    running = executor.is_running(deploy_id) or executor_llama.is_running(deploy_id)
    ep = executor_llama.endpoint(deploy_id, _spec_for(deploy_id))
    healthy = _probe_healthy(ep) if running else False
    return {
        "deploy_id": deploy_id,
        "running": running,
        "healthy": healthy,
        "endpoint": ep if running else None,
        "stage": executor_llama.stage(deploy_id) if running else executor.stage(deploy_id),
    }


@app.get("/deploys/{deploy_id}/logs")
async def deploy_logs(deploy_id: str, tail: int = 300, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    return {"logs": executor_llama.logs(deploy_id, tail=min(max(tail, 10), 5000))}


@app.get("/deploys/{deploy_id}/metrics")
async def deploy_metrics(deploy_id: str, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    if not (executor.is_running(deploy_id) or executor_llama.is_running(deploy_id)):
        raise HTTPException(status_code=404, detail="deploy not running")
    try:
        return await asyncio.to_thread(metrics.scrape, executor_llama.endpoint(deploy_id, _spec_for(deploy_id)))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"metrics unavailable: {e}") from e


@app.post("/deploys/{deploy_id}/stop")
async def deploy_stop(deploy_id: str, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    await asyncio.to_thread(executor.stop, deploy_id)
    await asyncio.to_thread(executor_llama.stop, deploy_id)
    return {"deploy_id": deploy_id, "status": "stopped"}
