from __future__ import annotations

import asyncio
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


@app.get("/capabilities")
async def capabilities():
    """What can this machine run? Used by the UI to pre-select the runtime."""
    gpu = _gpu()
    docker = _docker()
    return {
        "gpu": gpu,
        "docker": docker,
        "recommended_runtime": "vllm" if (gpu and docker) else "llama",
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


async def _deploy_job(deploy_id: str, spec: Spec) -> None:
    try:
        await asyncio.to_thread(_executor(spec.runtime).start, spec, deploy_id)
    except (executor.TransportError, executor_llama.TransportError):
        pass


@app.get("/deploys/{deploy_id}/status")
async def deploy_status(deploy_id: str, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    running = executor.is_running(deploy_id) or executor_llama.is_running(deploy_id)
    ep = executor_llama.endpoint(deploy_id)
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
        return await asyncio.to_thread(metrics.scrape, executor_llama.endpoint(deploy_id))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"metrics unavailable: {e}") from e


@app.post("/deploys/{deploy_id}/stop")
async def deploy_stop(deploy_id: str, x_agent_key: str | None = Header(None)):
    _require_key(x_agent_key)
    await asyncio.to_thread(executor.stop, deploy_id)
    await asyncio.to_thread(executor_llama.stop, deploy_id)
    return {"deploy_id": deploy_id, "status": "stopped"}
