from __future__ import annotations

import json
import threading
import uuid

from . import agent_client
from .db import Store, RouterSession

CONFIRMATIONS = 2

# ---- stage mode: rule-based routing (no LLM, zero latency) ----
STAGE_RULES = {
    "math": ["matemática", "matematica", "cálculo", "calculo", "equação", "equacao",
             "integral", "derivada", "álgebra", "algebra", "trigonometria", "prova", "teorema"],
    "code": ["código", "codigo", "programar", "função", "funcao", "bug", "script",
             "python", "javascript", "api", "funcao", "regex"],
    "reasoning": ["porquê", "porque", "explique", "explica", "raciocínio",
                  "raciocinio", "detalhes", "matemático", "matematico",
                  "física", "fisica", "prove", "teoria", "conceito", "fundamento"],
    "long": ["lista", "roteiro", "plano", "resumo completo", "dissertação", "dissertacao"],
}

STAGE_SIGNALS = [w for words in STAGE_RULES.values() for w in words]

JUDGE_PROMPT = """You are a routing judge. A weak model answered a user turn.
Read the user's request and the weak model's reply. Decide whether the weak
model is stuck, wrong, or unable to answer (needs escalation to a stronger
model) or whether its reply is acceptable.

Answer with ONLY valid JSON: {"escalate": true|false, "reason": "short reason"}"""

CLASSIFIER_PROMPT = """You are a model router. You pick which model answers a user turn.

Available models (id: description):
{candidates}

Read the user request and pick the single best model for it. Consider
strengths (math, code, vision, long answers, chat) and cost.

Answer with ONLY valid JSON: {{"choice": "<model_id>", "reason": "short reason"}}"""


def _content_of(result: dict) -> str:
    try:
        return result["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _reasoning_of(result: dict) -> str:
    try:
        return result["choices"][0]["message"].get("reasoning_content") or ""
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


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
    return ""


def _stage_wants_strong(messages: list[dict]) -> bool:
    import re
    text = _last_user_text(messages).lower()
    words = set(re.findall(r"[a-zà-ú]+", text))
    return bool(words & set(STAGE_SIGNALS))


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


def _chat(endpoint: str, model: str, messages: list[dict], max_tokens: int,
          temperature: float | None, stream: bool = False) -> dict:
    payload: dict = {"model": model, "messages": messages,
                     "max_tokens": max_tokens, "stream": stream}
    if temperature is not None:
        payload["temperature"] = temperature
    if stream:
        return payload
    return agent_client.chat(endpoint, payload)


def route_escalation(store: Store, pool, session: RouterSession, user_id: str,
                     messages: list[dict], max_tokens: int,
                     temperature: float | None) -> dict:
    """Default: weak responds, LLM judge decides, streak>=2 latches to strong."""
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
            "reasoning": _reasoning_of(result),
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
                "reasoning": _reasoning_of(result),
                "usage": _usage_of(result),
                "latched": True,
            }
        store.upsert_router_session(session)
        return {
            "served": weak.id,
            "decision": "weak_bad",
            "content": weak_content,
            "reasoning": _reasoning_of(weak_result),
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
                "reasoning": _reasoning_of(result),
                "usage": _usage_of(result),
                "latched": True,
            }
        store.upsert_router_session(session)
        return {
            "served": weak.id,
            "decision": "weak_ok",
            "content": weak_content,
            "reasoning": _reasoning_of(weak_result),
            "usage": _usage_of(weak_result),
            "latched": False,
        }

    session.streak = 0
    store.upsert_router_session(session)
    return {
        "served": weak.id,
        "decision": "weak_ok",
        "content": weak_content,
        "reasoning": _reasoning_of(weak_result),
        "usage": _usage_of(weak_result),
        "latched": False,
    }


def _run_judge_async(store: Store, pool, session_id: str, user_id: str,
                     messages: list[dict], weak_content: str) -> None:
    """Judge in background; updates the session latch for the next turn.
    Never raises (fails open)."""
    try:
        escalate = _judge(store, pool, session_id, user_id, messages, weak_content)
        store.apply_judge_verdict(session_id, escalate, CONFIRMATIONS)
    except Exception:
        pass


def route_advisor(store: Store, pool, session: RouterSession, user_id: str,
                  messages: list[dict], max_tokens: int,
                  temperature: float | None) -> dict:
    """Serve the weak reply immediately (zero added latency); judge runs in
    background and decides the latch for the NEXT turn."""
    weak = store.get(pool.weak_id)
    if weak is None or weak.endpoint is None:
        raise ValueError(f"weak deploy {pool.weak_id} has no endpoint")

    weak_payload = {"messages": messages, "max_tokens": max_tokens, "stream": False,
                    "model": weak.spec.model}
    if temperature is not None:
        weak_payload["temperature"] = temperature
    weak_result = agent_client.chat(weak.endpoint, weak_payload)
    weak_content = _content_of(weak_result)

    if session.latched:
        # latched: strong served, but still decide for next turn
        strong = store.get(pool.strong_id)
        result = agent_client.chat(strong.endpoint, {
            "messages": messages, "max_tokens": max_tokens, "stream": False,
            "model": strong.spec.model, **({"temperature": temperature} if temperature is not None else {}),
        })
        threading.Thread(
            target=_run_judge_async, args=(store, pool, session.id, user_id, messages, weak_content),
            daemon=True,
        ).start()
        return {
            "served": strong.id,
            "decision": "latched",
            "content": _content_of(result),
            "reasoning": _reasoning_of(result),
            "usage": _usage_of(result),
            "latched": True,
        }

    threading.Thread(
        target=_run_judge_async, args=(store, pool, session.id, user_id, messages, weak_content),
        daemon=True,
    ).start()
    return {
        "served": weak.id,
        "decision": "advisor",
        "content": weak_content,
        "reasoning": _reasoning_of(weak_result),
        "usage": _usage_of(weak_result),
        "latched": False,
    }


def route_stage(store: Store, pool, session: RouterSession, user_id: str,
                messages: list[dict], max_tokens: int,
                temperature: float | None) -> dict:
    """Rule-based routing (no LLM): keyword heuristics pick weak or strong."""
    weak = store.get(pool.weak_id)
    strong = store.get(pool.strong_id)
    if weak is None or weak.endpoint is None:
        raise ValueError(f"weak deploy {pool.weak_id} has no endpoint")
    if strong is None or strong.endpoint is None:
        raise ValueError(f"strong deploy {pool.strong_id} has no endpoint")

    if session.latched or _stage_wants_strong(messages):
        result = agent_client.chat(strong.endpoint, {
            "messages": messages, "max_tokens": max_tokens, "stream": False,
            "model": strong.spec.model, **({"temperature": temperature} if temperature is not None else {}),
        })
        return {
            "served": strong.id,
            "decision": "strong",
            "content": _content_of(result),
            "reasoning": _reasoning_of(result),
            "usage": _usage_of(result),
            "latched": session.latched,
        }
    result = agent_client.chat(weak.endpoint, {
        "messages": messages, "max_tokens": max_tokens, "stream": False,
        "model": weak.spec.model, **({"temperature": temperature} if temperature is not None else {}),
    })
    return {
        "served": weak.id,
        "decision": "weak",
        "content": _content_of(result),
        "reasoning": _reasoning_of(result),
        "usage": _usage_of(result),
        "latched": False,
    }


def route_round_robin(store: Store, pool, session: RouterSession, user_id: str,
                      messages: list[dict], max_tokens: int,
                      temperature: float | None) -> dict:
    """Alternate weak/strong per turn (round-robin load balancing)."""
    weak = store.get(pool.weak_id)
    strong = store.get(pool.strong_id)
    if weak is None or weak.endpoint is None:
        raise ValueError(f"weak deploy {pool.weak_id} has no endpoint")
    if strong is None or strong.endpoint is None:
        raise ValueError(f"strong deploy {pool.strong_id} has no endpoint")

    pick_strong = (session.streak % 2) == 1
    session.streak += 1
    store.upsert_router_session(session)
    target, model, decision = (strong, strong.spec.model, "strong") if pick_strong else (weak, weak.spec.model, "weak")
    result = agent_client.chat(target.endpoint, {
        "messages": messages, "max_tokens": max_tokens, "stream": False,
        "model": model, **({"temperature": temperature} if temperature is not None else {}),
    })
    return {
        "served": target.id,
        "decision": decision,
        "content": _content_of(result),
        "reasoning": _reasoning_of(result),
        "usage": _usage_of(result),
        "latched": False,
    }


def _pool_candidates(store: Store, pool) -> list:
    """Ordered deploy objects of the pool. Falls back to weak/strong for
    legacy pools without pool_models entries."""
    ids = store.get_pool_models(pool.id)
    if not ids:
        ids = [pool.weak_id, pool.strong_id]
    candidates = []
    for did in ids:
        deploy = store.get(did)
        if deploy is not None and deploy.endpoint:
            candidates.append(deploy)
    return candidates


def _classifier_choice(store: Store, pool, candidates: list, messages: list[dict]) -> str:
    """Ask the judge model which candidate answers. Returns a deploy id;
    falls back to the first candidate on any failure (fails open)."""
    judge_endpoint = None
    if pool.judge_id:
        judge = store.get(pool.judge_id)
        judge_endpoint = judge.endpoint if judge else None
    if judge_endpoint is None and candidates:
        judge_endpoint = candidates[0].endpoint

    lines = "\n".join(
        f"{c.id}: {c.spec.model}" for c in candidates
    )
    payload = {
        "model": "router-classifier",
        "messages": [
            {"role": "system", "content": CLASSIFIER_PROMPT.format(candidates=lines)},
            {"role": "user", "content": _last_user_text(messages)},
        ],
        "max_tokens": 120,
        "temperature": 0.0,
        "stream": False,
    }
    try:
        result = agent_client.chat(judge_endpoint, payload, timeout=60.0)
        raw = _content_of(result)
        verdict = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        choice = str(verdict.get("choice", ""))
        if any(c.id == choice for c in candidates):
            return choice
    except Exception:
        pass
    return candidates[0].id if candidates else None


def route_classifier(store: Store, pool, session: RouterSession, user_id: str,
                     messages: list[dict], max_tokens: int,
                     temperature: float | None) -> dict:
    """NVIDIA-style llm_classifier: a judge reads the request and picks the
    single best model among the pool's N candidates."""
    candidates = _pool_candidates(store, pool)
    if not candidates:
        raise ValueError(f"pool {pool.name} has no deploy with endpoint")

    chosen_id = _classifier_choice(store, pool, candidates, messages)
    chosen = next((c for c in candidates if c.id == chosen_id), candidates[0])
    result = agent_client.chat(chosen.endpoint, {
        "messages": messages, "max_tokens": max_tokens, "stream": False,
        "model": chosen.spec.model, **({"temperature": temperature} if temperature is not None else {}),
    })
    return {
        "served": chosen.id,
        "decision": "classifier",
        "content": _content_of(result),
        "reasoning": _reasoning_of(result),
        "usage": _usage_of(result),
        "latched": False,
    }


ROUTE_MODES = {
    "escalation": route_escalation,
    "advisor": route_advisor,
    "stage": route_stage,
    "round_robin": route_round_robin,
    "classifier": route_classifier,
}


def route_turn(store: Store, pool, session: RouterSession, user_id: str,
               messages: list[dict], max_tokens: int = 512,
               temperature: float | None = None) -> dict:
    """Run one routing turn against a pool, dispatching by pool.mode.

    Returns {"served": deploy_id, "decision": str, "content": str,
             "usage": dict, "latched": bool}.
    """
    handler = ROUTE_MODES.get(pool.mode, route_escalation)
    return handler(store, pool, session, user_id, messages, max_tokens, temperature)