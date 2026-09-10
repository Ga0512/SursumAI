#!/usr/bin/env python3
"""Step 3 — ask each judge whether the weak model's answer was good enough.

This is the question the benchmark exists to answer: is a small model enough to
tell that a smaller model failed? The judge never sees the gold answer — only
the question and the reply, exactly as it does in production.

Two judgements per item, because the router has two different judge jobs:

  escalate  binary, on an answer that already exists   (mode: escalation)
  choice    pick 1 of N from the question alone        (mode: classifier)

The second is the genuinely predictive one and is expected to be harder.

The prompts are imported from central/router.py, never copied: a benchmark of a
copied prompt measures the copy.

    python bench/run_judges.py --run bench/runs/gsm8k
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import BenchError, Deployment, Sink, utf8_output  # noqa: E402
from central.router import CLASSIFIER_PROMPT, JUDGE_PROMPT  # noqa: E402

# Same budget as production (central/router.py JUDGE_MAX_TOKENS). It used to be
# 120 there, too little for a model that reasons before answering: the verdict
# came back empty and the router read that as "do not escalate".
from central.router import JUDGE_MAX_TOKENS as JUDGE_BUDGET  # noqa: E402

JUDGES = [
    "Qwen/Qwen3-0.6B-GGUF",
    "Qwen/Qwen3-1.7B-GGUF",
    "Qwen/Qwen3-4B-GGUF",
]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise BenchError(f"missing {path} — run bench/run_models.py first")
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def parse_json(raw: str) -> dict:
    """The judge is asked for strict JSON and small models do not always
    comply. Failing to parse is not an error to raise — production treats an
    unreadable verdict as 'do not escalate' and serves the cheap answer, so the
    benchmark must score exactly that behaviour."""
    try:
        return json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return {}


class JudgeDown(BenchError):
    pass


_streak = 0


def _ask(dep, messages: list[dict]) -> str:
    """One judge call. An unparseable verdict is a verdict (production fails
    open on it); a transport error is not — it is skipped so a rerun retries
    it, and three in a row mean the judge's server is gone."""
    global _streak
    try:
        out, _ = dep.ask(messages, max_tokens=JUDGE_BUDGET)
    except BenchError as e:
        _streak += 1
        if _streak >= 3:
            raise JudgeDown(f"judge stopped answering: {str(e)[:160]}") from None
        raise
    _streak = 0
    return out


def judge_escalation(dep, question: str, reply: str) -> tuple[bool, str]:
    payload = json.dumps({"user_request": question, "weak_reply": reply},
                         ensure_ascii=False)
    out = _ask(dep, [{"role": "system", "content": JUDGE_PROMPT},
                     {"role": "user", "content": payload}])
    return bool(parse_json(out).get("escalate")), out


def judge_choice(dep, question: str, ladder: list[str]) -> tuple[str, str]:
    candidates = "\n".join(f"{m}: {m}" for m in ladder)
    out = _ask(dep, [
        {"role": "system", "content": CLASSIFIER_PROMPT.format(candidates=candidates)},
        {"role": "user", "content": question}])
    choice = str(parse_json(out).get("choice", ""))
    # fails open to the cheapest candidate, same as central/router.py
    return (choice if choice in ladder else ladder[0]), out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="bench/runs/gsm8k")
    ap.add_argument("--judges", nargs="*", default=JUDGES)
    ap.add_argument("--skip-classifier", action="store_true")
    args = ap.parse_args()
    utf8_output()

    run = Path(args.run)
    items = {r["item"]: r for r in load_jsonl(run / "items.jsonl")}
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    ladder = meta["ladder"]
    weak = ladder[0]

    weak_answers = {r["item"]: r for r in load_jsonl(run / "answers.jsonl")
                    if r["model"] == weak}
    if not weak_answers:
        raise BenchError(f"no answers from {weak} yet — run bench/run_models.py")

    verdicts = Sink(run / "verdicts.jsonl", ("item", "judge"))
    choices = Sink(run / "choices.jsonl", ("item", "judge"))
    print(f"{len(weak_answers)} answers from {weak} · {len(args.judges)} judges")

    try:
        for judge in args.judges:
            todo = [i for i in weak_answers if not verdicts.has(i, judge)]
            todo_c = ([] if args.skip_classifier
                      else [i for i in items if not choices.has(i, judge)])
            if not todo and not todo_c:
                print(f"  {judge}: already complete, skipping")
                continue

            with Deployment(judge, runtime="llama", max_tokens=JUDGE_BUDGET,
                            max_model_len=4096) as dep:
                for n, item in enumerate(todo, 1):
                    started = time.time()
                    try:
                        escalate, raw = judge_escalation(
                            dep, items[item]["question"], weak_answers[item]["answer"])
                    except JudgeDown:
                        raise
                    except BenchError:
                        continue
                    verdicts.write({
                        "item": item, "judge": judge, "target": weak,
                        "escalate": escalate, "raw": raw[:300],
                        "ms": round((time.time() - started) * 1000),
                    })
                    if n % 25 == 0 or n == len(todo):
                        print(f"    escalate {n}/{len(todo)}", flush=True)

                for n, item in enumerate(todo_c, 1):
                    try:
                        choice, raw = judge_choice(dep, items[item]["question"], ladder)
                    except JudgeDown:
                        raise
                    except BenchError:
                        continue
                    choices.write({"item": item, "judge": judge,
                                   "choice": choice, "raw": raw[:300]})
                    if n % 25 == 0 or n == len(todo_c):
                        print(f"    choice {n}/{len(todo_c)}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to continue where it stopped")
        return 130
    finally:
        verdicts.close()
        choices.close()

    meta["judges"] = args.judges
    (run / "meta.json").write_text(json.dumps(meta, indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
    print(f"\ndone — now: python bench/score.py --run {run}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
