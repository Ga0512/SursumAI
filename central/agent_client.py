from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from core import keys

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8010")
AGENT_KEY = keys.load_or_create_agent_key()


class AgentError(Exception):
    pass


def _request(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    url = AGENT_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Agent-Key", AGENT_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = ""
        raise AgentError(f"agent {method} {path}: HTTP {e.code} {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AgentError(f"agent unreachable at {AGENT_URL}: {e}") from None


def start(deploy_id: str, spec: dict) -> dict:
    return _request("POST", "/deploys", {"deploy_id": deploy_id, "spec": spec})


def preflight(spec: dict) -> dict:
    return _request("POST", "/preflight", {"spec": spec}, timeout=60.0)


def status(deploy_id: str) -> dict:
    return _request("GET", f"/deploys/{deploy_id}/status")


def capabilities() -> dict:
    return _request("GET", "/capabilities")


def model_fit(model: str, runtime: str) -> dict:
    from urllib.parse import quote
    return _request("GET", f"/model_fit?model={quote(model)}&runtime={runtime}")


def logs(deploy_id: str, tail: int = 300) -> str:
    return _request("GET", f"/deploys/{deploy_id}/logs?tail={tail}").get("logs", "")


def metrics(deploy_id: str) -> dict:
    return _request("GET", f"/deploys/{deploy_id}/metrics")


def chat(endpoint: str, payload: dict, timeout: float = 180.0,
         api_key: str | None = None) -> dict:
    """POST a chat completion to a deploy's OpenAI-compatible endpoint."""
    url = endpoint.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise AgentError(f"chat failed: HTTP {e.code} {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AgentError(f"deploy unreachable at {endpoint}: {e}") from None


def chat_stream(endpoint: str, payload: dict, timeout: float = 180.0,
                api_key: str | None = None):
    """Generator that forwards raw SSE bytes from a streaming chat completion.

    Runs inside the central's threadpool (StreamingResponse), so the blocking
    urllib loop does not stall the event loop.
    """
    url = endpoint.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                yield line
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        yield f'data: {json.dumps({"error": detail or f"HTTP {e.code}"})}\n\n'.encode()
    except (urllib.error.URLError, OSError) as e:
        yield f'data: {json.dumps({"error": f"deploy unreachable: {e}"})}\n\n'.encode()


def stop(deploy_id: str) -> None:
    _request("POST", f"/deploys/{deploy_id}/stop")
