#!/usr/bin/env python3
"""Step 2 — fill in the answer table.

For every model in the ladder, answer every item and record whether it was
right. This is the only expensive step, and it is expensive once: routing
policies are scored later by replaying this file, so adding or changing a
policy never costs GPU time again.

Models run strictly one at a time. On 6GB there is no other option, and it also
keeps the timings honest — nothing is competing for the card.

    python bench/run_models.py --run bench/runs/gsm8k
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dataset  # noqa: E402
from client import BenchError, Deployment, Sink, utf8_output  # noqa: E402

LADDER = [
    "Qwen/Qwen3-0.6B-GGUF",
    "Qwen/Qwen3-1.7B-GGUF",
    "Qwen/Qwen3-4B-GGUF",
    "Qwen/Qwen3-8B-GGUF",
]


def load_items(run: Path) -> list[dict]:
    path = run / "items.jsonl"
    if not path.exists():
        raise BenchError(f"no items at {path} — run bench/dataset.py first")
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ServerDown(BenchError):
    """The model server stopped answering; the deploy has to be replaced."""


def run_model(model: str, items: list[dict], sink: Sink, max_tokens: int,
              max_model_len: int) -> None:
    todo = [it for it in items if not sink.has(it["item"], model)]
    if not todo:
        print(f"  {model}: already complete, skipping")
        return
    print(f"  {model}: {len(todo)} items to go")

    with Deployment(model, runtime="llama", max_tokens=max_tokens,
                    max_model_len=max_model_len) as dep:
        hits = empty = done = streak = 0
        for it in todo:
            started = time.time()
            try:
                reply, info = dep.ask(
                    [{"role": "user",
                      "content": it["question"] + "\n\n" + it["instruction"]}],
                    max_tokens=max_tokens)
            except BenchError as e:
                # An infrastructure error is not an answer. Recording it as a
                # wrong one is how 70 instant 502s from a dead server once
                # passed for a model that "stopped knowing maths" at item 30.
                # Nothing is written, so a rerun retries the item.
                streak += 1
                print(f"    {it['item']}: {str(e)[:120]}", flush=True)
                if streak >= 3:
                    raise ServerDown(f"{model} stopped answering: {str(e)[:160]}") from None
                continue
            streak = 0

            ok = dataset.correct(reply, it["gold"])
            hits += ok
            done += 1
            empty += not reply
            sink.write({
                "item": it["item"],
                "model": model,
                "correct": bool(ok),
                "answer": reply,
                "extracted": dataset.extract(reply),
                "gold": it["gold"],
                "tokens": info.get("completion_tokens", 0),
                "finish_reason": info.get("finish_reason", ""),
                "reasoning_chars": info.get("reasoning_chars", 0),
                "ms": round((time.time() - started) * 1000),
            })
            if done % 25 == 0 or done == len(todo):
                note = f" · {empty} empty" if empty else ""
                print(f"    {done}/{len(todo)} · {hits}/{done} correct{note}", flush=True)

    if empty:
        # empty answers here are the model reasoning until the budget ran out:
        # a real failure of the model, reported so nobody mistakes it for infra
        print(f"  note: {model} used the whole budget without answering on {empty} items")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="bench/runs/gsm8k")
    ap.add_argument("--models", nargs="*", default=LADDER)
    # Qwen3 thinks before it answers and the thinking counts against the
    # budget. The pilot at 1024 cut half the answers off mid-reasoning: the
    # 0.6B scored 44% raw but 79% on the replies that got to finish. A budget
    # that truncates measures the budget, not the model.
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help="completion budget, reasoning included")
    ap.add_argument("--max-model-len", type=int, default=6144,
                    help="context window; must fit prompt + max-tokens")
    args = ap.parse_args()
    utf8_output()

    run = Path(args.run)
    items = load_items(run)
    sink = Sink(run / "answers.jsonl", ("item", "model"))
    print(f"{len(items)} items · {len(args.models)} models")

    # written up front, not at the end: a run interrupted halfway is still
    # worth scoring, and score.py needs the ladder to know who is weak
    meta_path = run / "meta.json"
    existing = (json.loads(meta_path.read_text(encoding="utf-8"))
                if meta_path.exists() else {})
    meta_path.write_text(json.dumps({
        "ladder": args.models,
        "judges": existing.get("judges", []),
        "dataset": "gsm8k",
        "items": len(items),
        "synthetic": False,
    }, indent=2) + "\n", encoding="utf-8", newline="\n")

    try:
        for model in args.models:
            # a server that dies mid-run is replaced, and the run carries on
            # from the first unanswered item
            for attempt in range(1, 4):
                try:
                    run_model(model, items, sink, args.max_tokens, args.max_model_len)
                    break
                except ServerDown as e:
                    print(f"  {e} — redeploying ({attempt}/3)", flush=True)
                    time.sleep(15)
            else:
                raise BenchError(f"{model} kept dying — giving up on it")
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to continue where it stopped")
        return 130
    finally:
        sink.close()

    print(f"\ndone — {run / 'answers.jsonl'}")
    print(f"next: python bench/run_judges.py --run {run}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
