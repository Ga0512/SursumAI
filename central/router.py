from __future__ import annotations

import json
import re
import threading
import unicodedata

from . import agent_client
from .db import Store, RouterSession

CONFIRMATIONS = 2

# ---- stage mode: rule-based routing (no LLM, zero latency) ----
# Bilingual on purpose: an English-only prompt used to never escalate, because
# every signal here was Portuguese. Accents are stripped before matching, so
# only the unaccented spelling needs to be listed.
STAGE_RULES = {
    "math": ["matematica", "calculo", "equacao", "integral", "derivada", "algebra",
             "trigonometria", "prova", "teorema",
             "math", "mathematics", "calculus", "equation", "derivative",
             "algebra", "geometry", "theorem", "proof", "solve"],
    "code": ["codigo", "programar", "funcao", "bug", "script", "python",
             "javascript", "api", "regex",
             "code", "coding", "program", "function", "debug", "compile",
             "refactor", "stacktrace", "traceback", "sql"],
    "reasoning": ["porque", "porquê", "explique", "explica", "raciocinio",
                  "detalhes", "matematico", "fisica", "teoria", "conceito",
                  "fundamento",
                  "why", "explain", "reasoning", "reason", "physics", "theory",
                  "concept", "analyze", "analyse", "compare", "derive"],
    "long": ["lista", "roteiro", "plano", "resumo", "dissertacao",
             "list", "outline", "plan", "essay", "summary", "step"],
}

STAGE_SIGNALS = {w for words in STAGE_RULES.values() for w in words}

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


def _served(deploy, decision: str, result: dict, latched: bool = False) -> dict:
    """Uniform outcome: who answered, with what, and why."""
    return {
        "served": deploy.id,
        "served_model": deploy.spec.model,
        "served_endpoint": deploy.endpoint,
        "decision": decision,
        "content": _content_of(result),
        "reasoning": _reasoning_of(result),
        "usage": _usage_of(result),
        "latched": latched,
    }


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


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def _stage_wants_strong(messages: list[dict]) -> bool:
    """Does the last user turn look like work for the strong model?

    Accents are stripped so 'matemática' and 'matematica' both hit, and the
    signal list carries both languages.
    """
    text = _strip_accents(_last_user_text(messages).lower())
    words = set(re.findall(r"[a-z0-9_]+", text))
    return bool(words & STAGE_SIGNALS)


def _judge_deploy(store: Store, pool):
    """The deploy that plays judge: the pool's judge if set, else the cheapest
    model in the pool (judging is a small, cheap call)."""
    if pool.judge_id:
        judge = store.get(pool.judge_id)
        if judge is not None and judge.endpoint:
            return judge
    candidates = _pool_candidates(store, pool)
    return candidates[0] if candidates else None


def _judge(store: Store, pool, session_id: str, user_id: str,
           messages: list[dict], weak_reply: str) -> bool:
    judge = _judge_deploy(store, pool)
    if judge is None:
        return False
    judge_endpoint = judge.endpoint
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
        result = agent_client.chat(judge_endpoint, payload, timeout=60.0,
                                   api_key=judge.spec.api_key)
    except agent_client.AgentError:
        return False
    try:
        raw = _content_of(result)
        verdict = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return bool(verdict.get("escalate"))
    except Exception:
        return False


def _ladder(store: Store, pool) -> list:
    """The pool's deploys, weakest first, all with a live endpoint.

    A pool holds N models, ordered by the user. Every mode reads that same
    ladder: the first entry is the cheap one, the last is the one worth
    escalating to, and round_robin walks the whole thing.
    """
    candidates = _pool_candidates(store, pool)
    if len(candidates) < 2:
        missing = [d for d in (store.get_pool_models(pool.id)
                               or [pool.weak_id, pool.strong_id]) if d]
        raise ValueError(
            f"pool '{pool.name}' needs at least 2 running models, found "
            f"{len(candidates)} of {len(missing)} — check that they are healthy"
        )
    return candidates


def _weak_strong(store: Store, pool):
    """The two ends of the ladder: cheapest and strongest."""
    ladder = _ladder(store, pool)
    return ladder[0], ladder[-1]


def _chat(deploy, messages: list[dict], max_tokens: int,
          temperature: float | None) -> dict:
    """One non-streaming completion against a deploy, with its own bearer key."""
    payload: dict = {
        "model": deploy.spec.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    return agent_client.chat(deploy.endpoint, payload, api_key=deploy.spec.api_key)


def route_escalation(store: Store, pool, session: RouterSession, user_id: str,
                     messages: list[dict], max_tokens: int,
                     temperature: float | None) -> dict:
    """Default: weak responds, LLM judge decides, streak>=2 latches to strong."""
    weak, strong = _weak_strong(store, pool)

    if session.latched:
        return _served(strong, "latched", _chat(strong, messages, max_tokens, temperature),
                       latched=True)

    weak_result = _chat(weak, messages, max_tokens, temperature)
    weak_content = _content_of(weak_result)
    bad = not _is_ok(weak_content, weak_result)
    escalate = bad or _judge(store, pool, session.id, user_id, messages, weak_content)

    if not escalate:
        session.streak = 0
        store.upsert_router_session(session)
        return _served(weak, "weak_ok", weak_result)

    session.streak += 1
    if session.streak >= CONFIRMATIONS:
        session.latched = True
        store.upsert_router_session(session)
        return _served(strong, "escalated", _chat(strong, messages, max_tokens, temperature),
                       latched=True)
    store.upsert_router_session(session)
    return _served(weak, "weak_bad" if bad else "weak_ok", weak_result)


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
    weak, strong = _weak_strong(store, pool)

    weak_result = _chat(weak, messages, max_tokens, temperature)
    weak_content = _content_of(weak_result)

    def _judge_later() -> None:
        threading.Thread(
            target=_run_judge_async,
            args=(store, pool, session.id, user_id, messages, weak_content),
            daemon=True,
        ).start()

    if session.latched:
        # latched: strong serves, but the judge still decides the next turn
        result = _chat(strong, messages, max_tokens, temperature)
        _judge_later()
        return _served(strong, "latched", result, latched=True)

    _judge_later()
    return _served(weak, "advisor", weak_result)


def route_stage(store: Store, pool, session: RouterSession, user_id: str,
                messages: list[dict], max_tokens: int,
                temperature: float | None) -> dict:
    """Rule-based routing (no LLM): keyword heuristics pick weak or strong."""
    weak, strong = _weak_strong(store, pool)
    if session.latched or _stage_wants_strong(messages):
        return _served(strong, "strong", _chat(strong, messages, max_tokens, temperature),
                       latched=session.latched)
    return _served(weak, "weak", _chat(weak, messages, max_tokens, temperature))


def route_round_robin(store: Store, pool, session: RouterSession, user_id: str,
                      messages: list[dict], max_tokens: int,
                      temperature: float | None) -> dict:
    """Take the next model in the pool, cycling through all N of them."""
    target, decision = _round_robin_pick(store, pool, session)
    return _served(target, decision, _chat(target, messages, max_tokens, temperature))


def _round_robin_pick(store: Store, pool, session: RouterSession):
    ladder = _ladder(store, pool)
    index = session.streak % len(ladder)
    session.streak += 1
    store.upsert_router_session(session)
    return ladder[index], f"round_robin[{index + 1}/{len(ladder)}]"


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


def _classifier_choice(store: Store, pool, candidates: list,
                       messages: list[dict]) -> str | None:
    """Ask the judge model which candidate answers. Returns a deploy id;
    falls back to the first candidate on any failure (fails open)."""
    judge = _judge_deploy(store, pool)
    if judge is None and candidates:
        judge = candidates[0]
    if judge is None:
        return None

    lines = "\n".join(f"{c.id}: {c.spec.model}" for c in candidates)
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
        result = agent_client.chat(judge.endpoint, payload, timeout=60.0,
                                   api_key=judge.spec.api_key)
        raw = _content_of(result)
        verdict = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        choice = str(verdict.get("choice", ""))
        if any(c.id == choice for c in candidates):
            return choice
    except Exception:
        pass
    return candidates[0].id if candidates else None


def _classifier_target(store: Store, pool, messages: list[dict]):
    candidates = _pool_candidates(store, pool)
    if not candidates:
        raise ValueError(f"pool {pool.name} has no deploy with an endpoint")
    chosen_id = _classifier_choice(store, pool, candidates, messages)
    return next((c for c in candidates if c.id == chosen_id), candidates[0])


def route_classifier(store: Store, pool, session: RouterSession, user_id: str,
                     messages: list[dict], max_tokens: int,
                     temperature: float | None) -> dict:
    """NVIDIA-style llm_classifier: a judge reads the request and picks the
    single best model among the N candidates of the pool."""
    chosen = _classifier_target(store, pool, messages)
    return _served(chosen, "classifier", _chat(chosen, messages, max_tokens, temperature))


ROUTE_MODES = {
    "escalation": route_escalation,
    "advisor": route_advisor,
    "stage": route_stage,
    "round_robin": route_round_robin,
    "classifier": route_classifier,
}


def pick_target(store: Store, pool, session: RouterSession,
                messages: list[dict]) -> tuple | None:
    """Decide who answers WITHOUT generating anything, when the mode allows it.

    Returns (deploy, decision) so the caller can stream tokens straight from
    the chosen model. Returns None for the modes that can only decide after
    reading a first reply (escalation and advisor while not latched) — those
    have to generate before they can route.
    """
    mode = pool.mode if pool.mode in ROUTE_MODES else "escalation"

    if session.latched and mode in ("escalation", "advisor", "stage"):
        _, strong = _weak_strong(store, pool)
        return strong, "latched"

    if mode == "stage":
        weak, strong = _weak_strong(store, pool)
        if _stage_wants_strong(messages):
            return strong, "strong"
        return weak, "weak"

    if mode == "round_robin":
        return _round_robin_pick(store, pool, session)

    if mode == "classifier":
        return _classifier_target(store, pool, messages), "classifier"

    return None


def route_turn(store: Store, pool, session: RouterSession, user_id: str,
               messages: list[dict], max_tokens: int = 512,
               temperature: float | None = None) -> dict:
    """Run one routing turn against a pool, dispatching by pool.mode.

    Returns {"served": deploy_id, "served_model": str, "served_endpoint": str,
             "decision": str, "content": str, "reasoning": str,
             "usage": dict, "latched": bool}.
    """
    handler = ROUTE_MODES.get(pool.mode, route_escalation)
    return handler(store, pool, session, user_id, messages, max_tokens, temperature)
