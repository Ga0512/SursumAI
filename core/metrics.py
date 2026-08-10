from __future__ import annotations

import re
import time
import urllib.request
from typing import Any

# vLLM lines look like:  vllm:request_success_total{...} 123
# llama.cpp lines look like:  llamacpp:requests_processing 0
LINE_RE = re.compile(r"^(?:\w+):([A-Za-z_][A-Za-z0-9_:]*)(?:\{.*\})?\s+([-+0-9.eE]+)$")


def _base(endpoint: str) -> str:
    return endpoint[:-3] if endpoint.rstrip("/").endswith("/v1") else endpoint.rstrip("/")


def _scrape(endpoint: str, timeout: float = 10.0) -> dict[str, float]:
    url = f"{_base(endpoint)}/metrics"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    values: dict[str, float] = {}
    for line in body.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        if name.endswith(("_bucket", "_created")):
            continue
        try:
            values[name] = values.get(name, 0.0) + float(raw)
        except ValueError:
            pass
    return values


def _rate(cur: float, prev: float, dt: float) -> float:
    if dt <= 0:
        return 0.0
    if cur < prev:
        return 0.0
    return (cur - prev) / dt


def scrape(endpoint: str) -> dict[str, Any]:
    """Scrape a live /metrics endpoint (vLLM or llama.cpp) into a unified summary."""
    raw = _scrape(endpoint)
    now = time.time()

    # llama.cpp has direct throughput gauges; vLLM has histogram sums/counts.
    if "predicted_tokens_seconds" in raw or "requests_processing" in raw:
        return _scrape_llama(raw, now)
    return _scrape_vllm(raw, now)


def _scrape_vllm(raw: dict[str, float], now: float) -> dict[str, Any]:
    ttf_sum = raw.get("time_to_first_token_seconds_sum", 0.0)
    ttf_cnt = raw.get("time_to_first_token_seconds_count", 0.0)
    out_sum = raw.get("request_time_per_output_token_seconds_sum", 0.0)
    out_cnt = raw.get("request_time_per_output_token_seconds_count", 0.0)
    e2e_sum = raw.get("e2e_request_latency_seconds_sum", 0.0)
    e2e_cnt = raw.get("e2e_request_latency_seconds_count", 0.0)
    prompt_sum = raw.get("request_prompt_tokens_sum", 0.0)
    prompt_cnt = raw.get("request_prompt_tokens_count", 0.0)
    gen_sum = raw.get("request_generation_tokens_sum", 0.0)
    gen_cnt = raw.get("request_generation_tokens_count", 0.0)

    def _avg(s: float, c: float) -> float:
        if c <= 0:
            return 0.0
        v = s / c
        return max(v, 0.0)

    return {
        "ts": now,
        "runtime": "vllm",
        "prompt_tokens_total": raw.get("prompt_tokens_total", 0.0),
        "generation_tokens_total": raw.get("generation_tokens_total", 0.0),
        "prompt_tokens_cached_total": raw.get("prompt_tokens_cached_total", 0.0),
        "requests_total": raw.get("request_success_total", 0.0),
        "requests_failed_total": raw.get("request_failed_total", 0.0),
        "preemptions_total": raw.get("num_preemptions_total", 0.0),
        "num_running": raw.get("num_requests_running", 0.0),
        "num_waiting": raw.get("num_requests_waiting", 0.0),
        "kv_cache_usage_perc": raw.get("kv_cache_usage_perc", 0.0),
        "ttft_avg_ms": _avg(ttf_sum, ttf_cnt) * 1000.0,
        "output_token_avg_ms": _avg(out_sum, out_cnt) * 1000.0,
        "e2e_latency_avg_ms": _avg(e2e_sum, e2e_cnt) * 1000.0,
        "prompt_tokens_avg": _avg(prompt_sum, prompt_cnt),
        "generation_tokens_avg": _avg(gen_sum, gen_cnt),
    }


def _scrape_llama(raw: dict[str, float], now: float) -> dict[str, Any]:
    return {
        "ts": now,
        "runtime": "llama",
        "prompt_tokens_total": raw.get("prompt_tokens_total", 0.0),
        "generation_tokens_total": raw.get("tokens_predicted_total", 0.0),
        "prompt_tokens_cached_total": 0.0,
        "requests_total": raw.get("requests_completed_total", 0.0),
        "requests_failed_total": raw.get("requests_failed_total", 0.0),
        "preemptions_total": 0.0,
        "num_running": raw.get("requests_processing", 0.0),
        "num_waiting": raw.get("requests_deferred", 0.0),
        "kv_cache_usage_perc": 0.0,
        "prompt_tokens_per_s": raw.get("prompt_tokens_seconds", 0.0),
        "generation_tokens_per_s": raw.get("predicted_tokens_seconds", 0.0),
    }


def derive(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Compute per-second rates (deltas) against the previous snapshot."""
    dt = current["ts"] - (previous["ts"] if previous else current["ts"])
    result: dict[str, Any] = {
        "runtime": current.get("runtime", "vllm"),
        "prompt_tokens": int(current["prompt_tokens_total"]),
        "generation_tokens": int(current["generation_tokens_total"]),
        "prompt_tokens_cached": int(current["prompt_tokens_cached_total"]),
        "requests": int(current["requests_total"]),
        "requests_failed": int(current["requests_failed_total"]),
        "preemptions": int(current["preemptions_total"]),
        "num_running": int(current["num_running"]),
        "num_waiting": int(current["num_waiting"]),
        "kv_cache_usage_perc": round(current["kv_cache_usage_perc"], 1),
        "ttft_avg_ms": round(current.get("ttft_avg_ms", 0.0), 1),
        "output_token_avg_ms": round(current.get("output_token_avg_ms", 0.0), 1),
        "e2e_latency_avg_ms": round(current.get("e2e_latency_avg_ms", 0.0), 1),
        "prompt_tokens_avg": round(current.get("prompt_tokens_avg", 0.0), 1),
        "generation_tokens_avg": round(current.get("generation_tokens_avg", 0.0), 1),
    }
    # llama.cpp exposes live throughput gauges directly.
    if current.get("runtime") == "llama":
        result["prompt_tokens_per_s"] = round(current.get("prompt_tokens_per_s", 0.0), 1)
        result["generation_tokens_per_s"] = round(current.get("generation_tokens_per_s", 0.0), 1)
        if previous and dt > 0:
            result["requests_per_s"] = round(_rate(current["requests_total"], previous["requests_total"], dt), 2)
        return result

    if previous:
        result["prompt_tokens_per_s"] = round(_rate(current["prompt_tokens_total"], previous["prompt_tokens_total"], dt), 1)
        result["generation_tokens_per_s"] = round(_rate(current["generation_tokens_total"], previous["generation_tokens_total"], dt), 1)
        result["requests_per_s"] = round(_rate(current["requests_total"], previous["requests_total"], dt), 2)
    return result
