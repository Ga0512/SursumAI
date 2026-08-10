from __future__ import annotations

import asyncio
import re
import time

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core import metrics
from core.spec import Spec, SpecError
from . import agent_client
from . import auth as authmod
from .db import DeployState, Store

app = FastAPI(title="SursumAI Central")
store = Store()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class DeployRequest(BaseModel):
    model: str
    runtime: str = "vllm"
    target: str = "local"
    gpus: int = 1
    nodes: int = 1
    gpu_memory_utilization: float = 0.50
    max_model_len: int = 300
    max_tokens: int = 2048
    temperature: float = 0.0
    hf_token: str = ""


class RedeployRequest(BaseModel):
    model: str | None = None
    runtime: str | None = None
    target: str | None = None
    gpus: int | None = None
    nodes: int | None = None
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    hf_token: str | None = None


class ChatRequest(BaseModel):
    messages: list[dict]
    max_tokens: int = 512
    temperature: float | None = None
    stream: bool = False


bearer = HTTPBearer(auto_error=False)


def _current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if creds is None:
        raise HTTPException(status_code=401, detail="authentication required")
    user = store.get_user_by_token(creds.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return user


def _spec_from_request(req: DeployRequest | RedeployRequest, base: Spec | None = None) -> Spec:
    data = req.model_dump(exclude_none=True)
    merged = {**base.to_dict(), **data} if base else data
    spec = Spec.from_dict(merged)
    spec.validate()
    return spec


def _get_owned_deploy(deploy_id: str, user_id: str):
    deploy = store.get(deploy_id)
    if deploy is None or deploy.user_id != user_id:
        raise HTTPException(status_code=404, detail="deploy not found")
    return deploy


async def _wait_healthy(deploy_id: str, timeout: int = 1800) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = await asyncio.to_thread(agent_client.status, deploy_id)
            if st.get("healthy"):
                return True
        except agent_client.AgentError:
            pass
        await asyncio.sleep(5)
    return False


async def _run_preflight(spec: Spec) -> list[dict] | None:
    try:
        result = await asyncio.to_thread(agent_client.preflight, spec.to_dict())
        return result.get("checks")
    except agent_client.AgentError:
        return None


async def _deploy_job(deploy_id: str) -> None:
    deploy = store.get(deploy_id)
    if deploy is None:
        return

    deploy.status = DeployState.CHECKING
    store.update(deploy)
    checks = await _run_preflight(deploy.spec)
    deploy = store.get(deploy_id)
    if deploy is None:
        return
    deploy.preflight = checks
    failed = [c for c in checks or [] if not c.get("ok")]
    if failed:
        deploy.status = DeployState.FAILED
        deploy.error = "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
        store.update(deploy)
        return

    deploy.status = DeployState.PROVISIONING
    store.update(deploy)
    try:
        await asyncio.to_thread(agent_client.start, deploy.id, deploy.spec.to_dict())
        deploy = store.get(deploy_id)
        if deploy is None:
            return
        ok = await _wait_healthy(deploy_id)
        deploy.status = DeployState.HEALTHY if ok else DeployState.FAILED
        if ok:
            try:
                st = await asyncio.to_thread(agent_client.status, deploy_id)
                deploy.endpoint = st.get("endpoint")
            except agent_client.AgentError:
                deploy.endpoint = None
        else:
            deploy.error = "Model did not become healthy in time"
    except agent_client.AgentError as e:
        deploy = store.get(deploy_id)
        if deploy is None:
            return
        deploy.status = DeployState.FAILED
        deploy.error = str(e)
    store.update(deploy)


def _latest_metrics(deploy_id: str) -> dict | None:
    latest = store.latest_metrics(deploy_id)
    if latest is None:
        return None
    return metrics.derive(latest, None)


def _spark(deploy_id: str, limit: int = 30) -> list[float] | None:
    snaps = store.list_metrics(deploy_id, limit=limit + 1)
    if len(snaps) < 2:
        return None
    out = []
    for cur, prev in zip(snaps[1:], snaps[:-1]):
        dt = cur["ts"] - prev["ts"]
        if dt > 0 and cur["generation_tokens_total"] >= prev["generation_tokens_total"]:
            out.append(round((cur["generation_tokens_total"] - prev["generation_tokens_total"]) / dt, 1))
        else:
            out.append(0.0)
    return out


async def _metrics_loop() -> None:
    while True:
        await asyncio.sleep(10)
        for d in store.list():
            if d.status not in (DeployState.HEALTHY, DeployState.REDEPLOYING, DeployState.PROVISIONING):
                continue
            if not d.endpoint:
                continue
            try:
                snap = await asyncio.to_thread(agent_client.metrics, d.id)
                store.save_metrics(d.id, snap)
            except (agent_client.AgentError, Exception):
                pass


def _reconcile_stale(statuses: set[str] | None = None) -> None:
    """Mark deploys as failed when the underlying process/container is gone."""
    if statuses is None:
        statuses = {DeployState.HEALTHY, DeployState.REDEPLOYING, DeployState.PROVISIONING}
    for d in store.list():
        if d.status not in statuses:
            continue
        try:
            st = agent_client.status(d.id)
        except agent_client.AgentError:
            continue
        if not st.get("running"):
            d.status = DeployState.FAILED
            d.error = "Container is no longer running on the agent"
            store.update(d)


async def _reconcile_loop() -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await asyncio.to_thread(_reconcile_stale, {DeployState.HEALTHY})
        except Exception:
            pass


@app.on_event("startup")
async def _startup() -> None:
    await asyncio.to_thread(_reconcile_stale)
    asyncio.create_task(_metrics_loop())
    asyncio.create_task(_reconcile_loop())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/meta/capabilities")
async def meta_capabilities():
    """Proxy the agent's capabilities so the UI can pre-select a runtime."""
    try:
        return await asyncio.to_thread(agent_client.capabilities)
    except agent_client.AgentError:
        return {"gpu": False, "docker": False, "recommended_runtime": "llama"}


# ---- auth ----

@app.post("/auth/register", status_code=201)
async def register(req: RegisterRequest):
    email = req.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")
    if len(req.password) < 8:
        raise HTTPException(status_code=422, detail="password must be at least 8 characters")
    if store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="email already registered")
    name = req.name.strip() or email.split("@")[0]
    user = store.create_user(email, name, authmod.hash_password(req.password))
    token = authmod.new_token()
    store.create_session(token, user.id, authmod.session_expiry())
    return {"token": token, "user": user.to_dict()}


@app.post("/auth/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    user = store.get_user_by_email(email)
    if user is None or not authmod.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = authmod.new_token()
    store.create_session(token, user.id, authmod.session_expiry())
    return {"token": token, "user": user.to_dict()}


@app.post("/auth/logout")
async def logout(creds: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if creds is not None:
        store.delete_session(creds.credentials)
    return {"status": "logged out"}


@app.get("/auth/me")
async def me(user=Depends(_current_user)):
    return user.to_dict()


# ---- deploys ----

@app.post("/deploys", status_code=201)
async def create_deploy(req: DeployRequest, user=Depends(_current_user)):
    try:
        spec = _spec_from_request(req)
    except SpecError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    deploy = store.create(spec, user.id)
    asyncio.create_task(_deploy_job(deploy.id))
    return deploy.to_dict()


@app.get("/deploys")
async def list_deploys(user=Depends(_current_user)):
    return [
        {**d.to_dict(), "metrics": _latest_metrics(d.id), "spark": _spark(d.id)}
        for d in store.list(user.id)
    ]


@app.get("/deploys/{deploy_id}")
async def get_deploy(deploy_id: str, user=Depends(_current_user)):
    deploy = _get_owned_deploy(deploy_id, user.id)
    out = {**deploy.to_dict(), "metrics": _latest_metrics(deploy_id), "spark": _spark(deploy_id)}
    if deploy.status in (DeployState.CHECKING, DeployState.PROVISIONING, DeployState.REDEPLOYING):
        try:
            st = await asyncio.to_thread(agent_client.status, deploy_id)
            out["stage"] = st.get("stage")
        except agent_client.AgentError:
            pass
    return out


@app.get("/deploys/{deploy_id}/metrics")
async def get_deploy_metrics(deploy_id: str, limit: int = 60, user=Depends(_current_user)):
    _get_owned_deploy(deploy_id, user.id)
    return {
        "deploy_id": deploy_id,
        "latest": _latest_metrics(deploy_id),
        "history": store.list_metrics(deploy_id, limit=min(max(limit, 1), 500)),
    }


@app.post("/deploys/{deploy_id}/redeploy")
async def redeploy(deploy_id: str, req: RedeployRequest, user=Depends(_current_user)):
    deploy = _get_owned_deploy(deploy_id, user.id)
    try:
        spec = _spec_from_request(req, base=deploy.spec)
    except SpecError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    deploy.spec = spec
    deploy.status = DeployState.REDEPLOYING
    deploy.error = None
    store.update(deploy)
    store.clear_metrics(deploy_id)
    asyncio.create_task(_deploy_job(deploy.id))
    return deploy.to_dict()


@app.get("/deploys/{deploy_id}/logs")
async def get_logs(deploy_id: str, tail: int = 300, user=Depends(_current_user)):
    _get_owned_deploy(deploy_id, user.id)
    try:
        content = await asyncio.to_thread(agent_client.logs, deploy_id, min(max(tail, 10), 5000))
    except agent_client.AgentError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"logs": content}


@app.post("/deploys/{deploy_id}/chat")
async def chat_with_deploy(deploy_id: str, req: ChatRequest, user=Depends(_current_user)):
    """Proxy a chat completion to the deploy's OpenAI-compatible endpoint.
    Supports streaming (SSE) when req.stream is true."""
    deploy = _get_owned_deploy(deploy_id, user.id)
    if not deploy.endpoint:
        raise HTTPException(status_code=409, detail="deploy has no endpoint yet")
    payload: dict = {
        "model": deploy.spec.model,
        "messages": req.messages,
        "max_tokens": req.max_tokens,
        "stream": req.stream,
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stream:
        return StreamingResponse(
            agent_client.chat_stream(deploy.endpoint, payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = await asyncio.to_thread(agent_client.chat, deploy.endpoint, payload)
    except agent_client.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


@app.delete("/deploys/{deploy_id}")
async def destroy_deploy(deploy_id: str, user=Depends(_current_user)):
    deploy = _get_owned_deploy(deploy_id, user.id)
    deploy.status = DeployState.DESTROYING
    store.update(deploy)
    try:
        await asyncio.to_thread(agent_client.stop, deploy.id)
    except agent_client.AgentError:
        pass
    store.delete(deploy_id)
    return {"status": "deleted"}
