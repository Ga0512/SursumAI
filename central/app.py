from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core import metrics
from core import ports
from core.spec import Spec, SpecError
from . import agent_client
from . import auth as authmod
from . import router as router_mod
from .db import DeployState, Pool, RouterSession, Store

log = logging.getLogger("sursumai.central")

store = Store()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.purge_expired_sessions()
    await asyncio.to_thread(_reconcile_stale)
    tasks = [asyncio.create_task(_metrics_loop()),
             asyncio.create_task(_reconcile_loop())]
    try:
        yield
    finally:
        # stop the background loops with the app, so a reload does not leave
        # two of each running against the same database
        for task in tasks:
            task.cancel()


app = FastAPI(title="SursumAI Central", lifespan=lifespan)

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


class PoolRequest(BaseModel):
    name: str
    weak_id: str | None = None
    strong_id: str | None = None
    judge_id: str | None = None
    model_ids: list[str] | None = None
    mode: str = "escalation"


class RouterChatRequest(BaseModel):
    model: str = "router"
    messages: list[dict]
    max_tokens: int = 512
    temperature: float | None = None
    stream: bool = False
    session_id: str | None = None


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
    except agent_client.AgentError as e:
        log.warning("preflight could not reach the agent: %s", e)
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
                if st.get("auth_enforced") is False:
                    # the runtime came up ignoring its key file: the endpoint is
                    # reachable by anyone who finds the port
                    log.error(
                        "deploy %s is serving WITHOUT authentication — the runtime "
                        "did not apply its API key. Endpoint %s is open to anything "
                        "that can reach the port.", deploy_id, deploy.endpoint,
                    )
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
            except agent_client.AgentError as e:
                # expected while a model is still warming up or already gone
                log.debug("metrics unavailable for %s: %s", d.id, e)
            except Exception:
                log.exception("unexpected error scraping metrics for %s", d.id)


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
            log.exception("reconcile pass failed")


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


@app.get("/meta/model_fit")
async def meta_model_fit(model: str, runtime: str = "llama"):
    """Proxy the agent's VRAM/model fit suggestion for the deploy form."""
    try:
        return await asyncio.to_thread(agent_client.model_fit, model, runtime)
    except agent_client.AgentError:
        return {"ok": False, "reason": "unavailable"}


VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
REPO_BASE = "https://github.com/Ga0512/SursumAI"


def _local_version() -> str:
    try:
        return VERSION_FILE.read_text().strip() or "?"
    except OSError:
        return "?"


LATEST_RELEASE_API = "https://api.github.com/repos/Ga0512/SursumAI/releases/latest"


def _latest_version() -> str:
    """The newest published release tag (v1.2.3 -> 1.2.3). Releases, not the
    branch tip: the installer only ever installs a tag."""
    try:
        with urllib.request.urlopen(LATEST_RELEASE_API, timeout=15) as resp:
            tag = (json.load(resp).get("tag_name") or "").strip()
            return tag.lstrip("v") or "?"
    except Exception as e:
        log.info("could not check for updates: %s", e)
        return "?"


@app.get("/meta/update")
async def meta_update():
    """Check whether a newer SursumAI version is available."""
    current = _local_version()
    latest = await asyncio.to_thread(_latest_version)
    return {
        "current": current,
        "latest": latest,
        "update_available": latest not in ("?", current),
    }


@app.post("/meta/update")
async def meta_update_apply():
    """Re-run the installer in the background to update the codebase.
    The installer ends by starting SursumAI, so the services come back up
    on the new version automatically."""
    import subprocess as sp
    latest = await asyncio.to_thread(_latest_version)
    if latest == "?":
        raise HTTPException(status_code=502, detail="could not find a published release to update to")
    tag = f"v{latest}"
    # fetch the installer from the tag being installed, never from the branch
    script = f"{REPO_BASE}/raw/{tag}/install.sh"
    try:
        sp.Popen(
            ["bash", "-c", f"curl -fsSL {script} | SURSUMAI_VERSION={tag} bash"],
            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"update failed to start: {e}") from e
    return {"status": "started", "version": latest,
            "note": "SursumAI will restart after the update"}


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
    # every deploy gets its own bearer key; the runtime is started with
    # --api-key so the model endpoint is not open to whoever finds the port
    spec.api_key = authmod.new_deploy_key()
    try:
        # store.create allocates the port under a lock; the UNIQUE index is
        # the last word, so two simultaneous creates cannot share one
        deploy = store.create(spec, user.id)
    except ports.NoPortAvailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
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
    spec.api_key = deploy.spec.api_key or authmod.new_deploy_key()
    # keep the port across a redeploy (the endpoint URL must not move); a
    # legacy deploy without one gets a real port now
    if ports.in_range(deploy.spec.port):
        spec.port = deploy.spec.port
    else:
        try:
            spec.port = store.allocate_port(exclude_id=deploy.id)
        except ports.NoPortAvailable as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
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
            agent_client.chat_stream(deploy.endpoint, payload,
                                     api_key=deploy.spec.api_key),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = await asyncio.to_thread(
            agent_client.chat, deploy.endpoint, payload, 180.0, deploy.spec.api_key,
        )
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


# ---- pools (model routing) ----

def _get_owned_pool(pool_id: str, user_id: str) -> Pool:
    pool = store.get_pool(pool_id)
    if pool is None or pool.user_id != user_id:
        raise HTTPException(status_code=404, detail="pool not found")
    return pool


def _check_pool_members(pool: Pool, user_id: str) -> None:
    deploy_ids = store.get_pool_models(pool.id)
    if not deploy_ids:
        deploy_ids = [d for d in (pool.weak_id, pool.strong_id, pool.judge_id) if d]
    for deploy_id in deploy_ids:
        deploy = store.get(deploy_id)
        if deploy is None or deploy.user_id != user_id:
            raise HTTPException(status_code=422, detail=f"deploy {deploy_id} not found")
        if deploy.status != DeployState.HEALTHY or not deploy.endpoint:
            raise HTTPException(status_code=422, detail=f"deploy {deploy_id} is not healthy")


@app.get("/pools")
async def list_pools(user=Depends(_current_user)):
    out = []
    for p in store.list_pools(user.id):
        d = p.to_dict()
        d["model_ids"] = store.get_pool_models(p.id) or [p.weak_id, p.strong_id]
        out.append(d)
    return out


def _resolve_pool_args(req: PoolRequest, user: object, for_update: bool = False):
    """Validate PoolRequest and return (name, model_ids, judge_id, mode)."""
    if req.mode not in ("escalation", "advisor", "stage", "round_robin", "classifier"):
        raise HTTPException(status_code=422, detail="invalid pool mode")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="pool name required")
    if any(p.name == name and p.id != getattr(req, "_pool_id", None)
           for p in store.list_pools(user.id)):
        raise HTTPException(status_code=422, detail=f"pool name '{name}' already in use")
    if req.model_ids:
        model_ids = req.model_ids
        if len(model_ids) < 2:
            raise HTTPException(status_code=422, detail="pool needs at least 2 models")
        if len(set(model_ids)) != len(model_ids):
            raise HTTPException(status_code=422, detail="duplicate models in pool")
    else:
        if not req.weak_id or not req.strong_id:
            raise HTTPException(status_code=422, detail="model_ids or weak_id+strong_id required")
        if req.weak_id == req.strong_id:
            raise HTTPException(status_code=422, detail="weak and strong must be different deploys")
        model_ids = [req.weak_id, req.strong_id]
    return name, model_ids, req.judge_id, req.mode


@app.post("/pools", status_code=201)
async def create_pool(req: PoolRequest, user=Depends(_current_user)):
    name, model_ids, judge_id, mode = _resolve_pool_args(req, user)
    weak_id, strong_id = model_ids[0], model_ids[1]

    pool = store.create_pool(user.id, name, weak_id, strong_id, judge_id, mode)
    try:
        store.replace_pool_models(pool.id, model_ids)
        _check_pool_members(pool, user.id)
    except HTTPException:
        store.delete_pool(pool.id)
        raise
    out = pool.to_dict()
    out["model_ids"] = store.get_pool_models(pool.id)
    return out


@app.put("/pools/{pool_id}")
async def update_pool(pool_id: str, req: PoolRequest, user=Depends(_current_user)):
    pool = _get_owned_pool(pool_id, user.id)
    req._pool_id = pool_id
    name, model_ids, judge_id, mode = _resolve_pool_args(req, user)
    weak_id, strong_id = model_ids[0], model_ids[1]

    pool.name = name
    pool.weak_id = weak_id
    pool.strong_id = strong_id
    pool.judge_id = judge_id
    pool.mode = mode
    store.update_pool(pool)
    store.replace_pool_models(pool.id, model_ids)
    try:
        _check_pool_members(pool, user.id)
    except HTTPException:
        raise
    out = pool.to_dict()
    out["model_ids"] = store.get_pool_models(pool.id)
    return out


@app.delete("/pools/{pool_id}")
async def destroy_pool(pool_id: str, user=Depends(_current_user)):
    _get_owned_pool(pool_id, user.id)
    store.delete_pool(pool_id)
    return {"status": "deleted"}


@app.get("/pools/{pool_id}/log")
async def pool_log(pool_id: str, user=Depends(_current_user)):
    _get_owned_pool(pool_id, user.id)
    return store.list_router_log(pool_id=pool_id)


# ---- OpenAI-compatible router endpoint ----

def _openai_chunk(created: int, model: str, content: str | None = None,
                  reasoning: str | None = None,
                  finish: str | None = None) -> dict:
    delta: dict = {"role": "assistant"}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    chunk: dict = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return chunk


def _served_label(pool: Pool, model: str, decision: str) -> str:
    """What the client sees in `model`: the pool, the model that actually
    answered, and why it was picked."""
    return f"{pool.name} → {model} ({decision})"


CHUNK_SIZE = 80


def _replay_chunks(text: str, created: int, label: str, reasoning: bool = False):
    """Emit an already-generated answer as SSE deltas. Each chunk is the next
    slice of the text — never a growing prefix, which would make the client
    render the answer over and over."""
    for i in range(0, len(text), CHUNK_SIZE):
        piece = text[i:i + CHUNK_SIZE]
        chunk = (_openai_chunk(created, label, reasoning=piece) if reasoning
                 else _openai_chunk(created, label, piece))
        yield f"data: {json.dumps(chunk)}\n\n"


def _relabel_upstream(raw: bytes, label: str, session_id: str) -> bytes | None:
    """Rewrite `model` on a chunk coming straight from the chosen deploy so
    the client always sees which model answered. Returns None for [DONE],
    which the caller emits itself after the trailer."""
    line = raw.decode("utf-8", errors="replace")
    if not line.strip():
        return b"\n"
    if not line.startswith("data: "):
        return raw
    body = line[6:].strip()
    if body == "[DONE]":
        return None
    try:
        obj = json.loads(body)
    except ValueError:
        return raw
    obj["model"] = label
    obj["session_id"] = session_id
    return f"data: {json.dumps(obj)}\n\n".encode()


def _iter_router_stream(store: Store, pool: Pool, session: RouterSession,
                        user_id: str, messages: list[dict], created: int,
                        max_tokens: int, temperature: float | None):
    """Stream a routed answer.

    When the mode can decide who answers before generating (stage,
    round_robin, classifier, or a latched session), tokens are forwarded live
    from the chosen deploy. The escalation/advisor modes have to read a first
    reply before they can route, so their answer is already complete by the
    time we get here and is replayed in chunks.
    """
    try:
        target = router_mod.pick_target(store, pool, session, messages)
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return

    if target is not None:
        deploy, decision = target
        label = _served_label(pool, deploy.spec.model, decision)
        payload: dict = {
            "model": deploy.spec.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        for raw in agent_client.chat_stream(deploy.endpoint, payload,
                                            api_key=deploy.spec.api_key):
            out = _relabel_upstream(raw, label, session.id)
            if out is None:
                break
            yield out
        yield "data: [DONE]\n\n"
        store.log_router(session.id, pool.id, user_id, deploy.id, decision, 0, 0)
        return

    try:
        outcome = router_mod.route_turn(
            store, pool, session, user_id, messages, max_tokens, temperature,
        )
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return

    label = _served_label(pool, outcome["served_model"], outcome["decision"])
    reasoning = outcome.get("reasoning") or ""
    if reasoning:
        yield from _replay_chunks(reasoning, created, label, reasoning=True)
    yield from _replay_chunks(outcome["content"], created, label)
    final = _openai_chunk(created, label, finish="stop")
    final["usage"] = outcome["usage"]
    final["session_id"] = session.id
    final["sursumai"] = {
        "pool": pool.name,
        "served_deploy": outcome["served"],
        "served_model": outcome["served_model"],
        "decision": outcome["decision"],
    }
    final["choices"][0]["delta"] = {}
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"
    store.log_router(session.id, pool.id, user_id, outcome["served"],
                     outcome["decision"], outcome["usage"].get("total_tokens", 0), 0)


def _resolve_pool(user: object, model: str) -> Pool:
    if model == "router":
        pools = store.list_pools(user.id)
        if not pools:
            raise HTTPException(status_code=422, detail="no pool configured")
        return pools[0]
    pool = store.get_pool(model)
    if pool is None or pool.user_id != user.id:
        for candidate in store.list_pools(user.id):
            if candidate.name == model:
                pool = candidate
                break
    if pool is None or pool.user_id != user.id:
        raise HTTPException(status_code=404, detail=f"pool '{model}' not found")
    return pool


SESSION_TTL_SECONDS = 3600
SESSION_ID_MAX = 128


def _session_expired(session: RouterSession) -> bool:
    try:
        from datetime import datetime
        updated = datetime.fromisoformat(session.updated_at)
        if updated.tzinfo is None:
            from datetime import timezone
            updated = updated.replace(tzinfo=timezone.utc)
        age = time.time() - updated.timestamp()
        return age > SESSION_TTL_SECONDS
    except (ValueError, TypeError, OSError) as e:
        log.warning("unreadable router session timestamp %r: %s", session.updated_at, e)
        return False


@app.post("/v1/chat/completions")
async def router_chat(req: RouterChatRequest, user=Depends(_current_user)):
    """OpenAI-compatible router endpoint. model="router" uses the user's
    default pool; model="<pool_id>" targets a specific pool.

    session_id may be any stable string (e.g. "harness:meu-pipeline"); the
    central keeps the routing state (streak/latch) under that key, scoped to
    the user + pool. Sessions expire after inactivity (latch resets)."""
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages required")
    if req.session_id is not None and len(req.session_id) > SESSION_ID_MAX:
        raise HTTPException(status_code=422, detail="session_id too long")
    pool = _resolve_pool(user, req.model)
    session_id = req.session_id or uuid.uuid4().hex
    session = store.get_router_session(session_id)
    if session is None or session.pool_id != pool.id or session.user_id != user.id:
        session = RouterSession(id=session_id, pool_id=pool.id, user_id=user.id)
        store.upsert_router_session(session)
    elif _session_expired(session):
        session.streak = 0
        session.latched = False
        store.upsert_router_session(session)
    store.touch_router_session(session.id)
    created = int(time.time())
    if req.stream:
        return StreamingResponse(
            _iter_router_stream(store, pool, session, user.id, req.messages,
                                created, req.max_tokens, req.temperature),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        outcome = await asyncio.to_thread(
            router_mod.route_turn, store, pool, session, user.id, req.messages,
            req.max_tokens, req.temperature,
        )
    except Exception as e:
        log.exception("router turn failed for pool %s", pool.id)
        raise HTTPException(status_code=502, detail=str(e)) from e
    store.log_router(session.id, pool.id, user.id, outcome["served"],
                     outcome["decision"], outcome["usage"].get("total_tokens", 0), 0)
    message: dict = {"role": "assistant", "content": outcome["content"]}
    if outcome.get("reasoning"):
        message["reasoning_content"] = outcome["reasoning"]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": _served_label(pool, outcome["served_model"], outcome["decision"]),
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "stop"}],
        "usage": outcome["usage"],
        "session_id": session.id,
        "sursumai": {
            "pool": pool.name,
            "served_deploy": outcome["served"],
            "served_model": outcome["served_model"],
            "decision": outcome["decision"],
        },
    }


@app.get("/v1/models")
async def router_models(user=Depends(_current_user)):
    pools = store.list_pools(user.id)
    data = []
    for p in pools:
        members = []
        for deploy_id in (store.get_pool_models(p.id) or [p.weak_id, p.strong_id]):
            deploy = store.get(deploy_id)
            if deploy is not None:
                members.append({"deploy_id": deploy.id, "model": deploy.spec.model})
        data.append({
            "id": p.id,
            "object": "model",
            "owned_by": "sursumai",
            "pool": p.to_dict(),
            "models": members,
        })
    if data:
        data.insert(0, {"id": "router", "object": "model", "owned_by": "sursumai",
                        "pool": pools[0].to_dict()})
    return {"object": "list", "data": data}
