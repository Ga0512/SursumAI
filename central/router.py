from __future__ import annotations

import json
import uuid

from . import agent_client
from .db import Store, RouterSession

CONFIRMATIONS = 2

JUDGE_PROMPT = """You are a routing judge. A weak model answered a user turn.
Read the user's request and the weak model's reply. Decide whether the weak
model is stuck, wrong, or unable to answer (needs escalation to a stronger
model) or whether its reply is acceptable.

Answer with ONLY valid JSON: {"escalate": true|false, "reason": "short reason"}"""


def _content_of(result: dict) -> str:
    try:
        return result["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _usage_of(result: dict) -> dict:
    return result.get("usage") or {}


def _is_ok(reply: str, result: dict) -> bool:
    if not reply.strip():
        return False
    if result.get("error"):
        return False
    return True


def _judge(store: Store, pool, session_id: str, user_id: str,
           messages: list[dict], weak_reply: str) -> bool:
    judge_endpoint = None
    if pool.judge_id:
        judge = store.get(pool.judge_id)
        judge_endpoint = judge.endpoint if judge else None
    if judge_endpoint is None:
        weak = store.get(pool.weak_id)
        judge_endpoint = weak.endpoint if weak else None
    if judge_endpoint is None:
        return False
    payload = {
        "model": "judge",
        "messages": [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": json.dumps({
                "user_request": messages[-1].get("content", "") if messages else "",
                "weak_reply": weak_reply,
            }, ensure_ascii=False)},
        ],
        "max_tokens": 120,
        "temperature": 0.0,
        "stream": False,
    }
    try:
        result = agent_client.chat(judge_endpoint, payload, timeout=60.0)
    except agent_client.AgentError:
        return False
    try:
        raw = _content_of(result)
        verdict = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return bool(verdict.get("escalate"))
    except Exception:
        return False


def _decision_label(latched: bool, escalated: bool, served: str) -> str:
    if latched:
        return "latched"
    return "escalated" if escalated else "weak_ok"


def route_turn(store: Store, pool, session: RouterSession, user_id: str,
               messages: list[dict], max_tokens: int = 512,
               temperature: float | None = None) -> dict:
    """Run one escalation-routing turn against a pool.

    Returns {"served": deploy_id, "decision": str, "content": str,
             "usage": dict, "latched": bool}.
    """
    weak = store.get(pool.weak_id)
    strong = store.get(pool.strong_id)
    if weak is None or weak.endpoint is None:
        raise ValueError(f"weak deploy {pool.weak_id} has no endpoint")
    if strong is None or strong.endpoint is None:
        raise ValueError(f"strong deploy {pool.strong_id} has no endpoint")

    base = {"messages": messages, "max_tokens": max_tokens, "stream": False}
    if temperature is not None:
        base["temperature"] = temperature

    if session.latched:
        result = agent_client.chat(strong.endpoint, {**base, "model": strong.spec.model})
        return {
            "served": strong.id,
            "decision": _decision_label(True, False, strong.id),
            "content": _content_of(result),
            "usage": _usage_of(result),
            "latched": True,
        }

    weak_payload = {**base, "model": weak.spec.model}
    weak_result = agent_client.chat(weak.endpoint, weak_payload)
    weak_content = _content_of(weak_result)

    if not _is_ok(weak_content, weak_result):
        session.streak += 1
        if session.streak >= CONFIRMATIONS:
            session.latched = True
            result = agent_client.chat(strong.endpoint, {**base, "model": strong.spec.model})
            store.upsert_router_session(session)
            return {
                "served": strong.id,
                "decision": "escalated",
                "content": _content_of(result),
                "usage": _usage_of(result),
                "latched": True,
            }
        store.upsert_router_session(session)
        return {
            "served": weak.id,
            "decision": "weak_bad",
            "content": weak_content,
            "usage": _usage_of(weak_result),
            "latched": False,
        }

    escalate = _judge(store, pool, session.id, user_id, messages, weak_content)
    if escalate:
        session.streak += 1
        if session.streak >= CONFIRMATIONS:
            session.latched = True
            result = agent_client.chat(strong.endpoint, {**base, "model": strong.spec.model})
            store.upsert_router_session(session)
            return {
                "served": strong.id,
                "decision": "escalated",
                "content": _content_of(result),
                "usage": _usage_of(result),
                "latched": True,
            }
        store.upsert_router_session(session)
        return {
            "served": weak.id,
            "decision": "weak_ok",
            "content": weak_content,
            "usage": _usage_of(weak_result),
            "latched": False,
        }

    session.streak = 0
    store.upsert_router_session(session)
    return {
        "served": weak.id,
        "decision": "weak_ok",
        "content": weak_content,
        "usage": _usage_of(weak_result),
        "latched": False,
    }