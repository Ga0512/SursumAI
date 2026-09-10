#!/usr/bin/env python3
"""Talk to a running SursumAI the way any other client would.

The benchmark deploys, chats and destroys through the public API — no direct
imports of the store, no side door. A benchmark that reaches around the product
is not measuring the product.

Deploying needs a *session* token, not an account API key: an API key is scoped
to /v1 on purpose, so it can never create or destroy anything. Run
`sursumai login` once and this picks the token up from ~/.config/sursumai/auth.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CENTRAL = os.environ.get("SURSUMAI_CENTRAL", "http://localhost:8001").rstrip("/")
# same file `sursumai login` writes (sursumai/bin/sursumai: CONFIG_DIR)
AUTH_FILE = Path.home() / ".config" / "sursumai" / "auth.json"

READY_TIMEOUT = float(os.environ.get("BENCH_READY_TIMEOUT", 1800))  # model pulls are slow


class BenchError(RuntimeError):
    pass


def utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def token() -> str:
    if not AUTH_FILE.exists():
        raise BenchError(f"not logged in — run `sursumai login` (looked in {AUTH_FILE})")
    tok = json.loads(AUTH_FILE.read_text(encoding="utf-8")).get("token", "")
    if not tok:
        raise BenchError("stored login is empty — run `sursumai login`")
    return tok


def call(method: str, path: str, body: dict | None = None,
         timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CENTRAL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise BenchError(f"{method} {path} -> {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise BenchError(f"cannot reach {CENTRAL} — is SursumAI running? ({e.reason})") from None
    except (TimeoutError, OSError) as e:
        # a socket that times out mid-read is not a URLError; unwrapped, it
        # killed a multi-hour run instead of costing one item
        raise BenchError(f"{method} {path} -> {type(e).__name__}: {e}") from None
    return json.loads(raw) if raw.strip() else {}


# --- deployments ----------------------------------------------------------------

def deploy(model: str, runtime: str = "llama", max_model_len: int = 2048,
           max_tokens: int = 1024) -> dict:
    return call("POST", "/deploys", {
        "model": model,
        "runtime": runtime,
        "target": "local",
        "gpus": 1,
        "max_model_len": max_model_len,
        "max_tokens": max_tokens,
        "temperature": 0.0,          # the benchmark must be reproducible
    })


def wait_healthy(deploy_id: str, timeout: float = READY_TIMEOUT) -> dict:
    """Block until the deploy is serving, or explain why it never will."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        d = call("GET", f"/deploys/{deploy_id}")
        status = d.get("status", "?")
        if status != last:
            print(f"    {status}", flush=True)
            last = status
        if status == "healthy":
            return d
        if status == "failed":
            raise BenchError(f"deploy failed: {d.get('error') or 'no reason given'}")
        time.sleep(5)
    raise BenchError(f"deploy still {last} after {timeout:.0f}s")


def destroy(deploy_id: str) -> None:
    try:
        call("DELETE", f"/deploys/{deploy_id}")
    except BenchError as e:
        print(f"    warning: could not destroy {deploy_id}: {e}", file=sys.stderr)


class Deployment:
    """One model, alive only for as long as the benchmark needs it.

    6GB of VRAM holds one model at a time, so every phase is strictly
    sequential: bring it up, use it, tear it down, next.
    """

    def __init__(self, model: str, runtime: str = "llama", **kw):
        self.model, self.runtime, self.kw = model, runtime, kw
        self.id = ""

    def __enter__(self) -> "Deployment":
        for attempt in (1, 2):
            print(f"  deploying {self.model} ({self.runtime})", flush=True)
            self.id = deploy(self.model, self.runtime, **self.kw)["id"]
            try:
                wait_healthy(self.id)
                return self
            except BaseException as e:
                # __exit__ never runs when __enter__ raises, so a deploy that
                # failed its preflight would be left behind in the dashboard
                destroy(self.id)
                self.id = ""
                # the previous model's server can still be letting go of the
                # port when the next preflight checks it; one wait covers it
                if attempt == 1 and "port" in str(e):
                    print("    port still held by the previous model, waiting 30s",
                          flush=True)
                    time.sleep(30)
                    continue
                raise
        return self

    def __exit__(self, *exc) -> None:
        if self.id:
            print(f"  destroying {self.model}", flush=True)
            destroy(self.id)

    def ask(self, messages: list[dict], max_tokens: int = 1024,
            timeout: float = 960.0) -> tuple[str, dict]:
        """One completion.

        Returns (text, info). `info` carries usage plus `finish_reason` and the
        size of any reasoning block, because Qwen3 thinks by default and the
        thinking counts against max_tokens: a model can burn the whole budget
        reasoning and return empty content. Scored naively that reads as a
        wrong answer when it is really a truncated one, so the run has to be
        able to tell the two apart afterwards.
        """
        out = call("POST", f"/deploys/{self.id}/chat",
                   {"messages": messages, "max_tokens": max_tokens,
                    "temperature": 0.0},
                   timeout=timeout)
        try:
            choice = out["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            finish = choice.get("finish_reason") or ""
        except (KeyError, IndexError, TypeError):
            text, reasoning, finish = "", "", ""
        info = dict(out.get("usage") or {})
        info["finish_reason"] = finish
        info["reasoning_chars"] = len(reasoning)
        return text, info


# --- resumable output -----------------------------------------------------------

class Sink:
    """Append-only jsonl that remembers what is already done.

    Phase 2 runs for hours. Losing three hours to a crash at item 280, or to a
    laptop lid, is not acceptable, so every row is flushed as it is produced and
    a rerun skips what it already has.
    """

    def __init__(self, path: Path, key_fields: tuple[str, ...]):
        self.path = path
        self.key_fields = key_fields
        self.done: set[tuple] = set()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        row = json.loads(line)
                        self.done.add(tuple(row[k] for k in key_fields))
        self.fh = path.open("a", encoding="utf-8", newline="\n")

    def has(self, *key) -> bool:
        return tuple(key) in self.done

    def write(self, row: dict) -> None:
        self.fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.done.add(tuple(row[k] for k in self.key_fields))

    def close(self) -> None:
        self.fh.close()
