#!/usr/bin/env python3
"""Fetch GSM8K and turn it into the benchmark's item list.

GSM8K is the one slice that is both free-form and checkable by machine: the
model has to reason across several lines, but the verdict is a number compared
to a number. No LLM grades anything here — using a model to decide whether a
model answered well is exactly the circularity this benchmark exists to avoid.

    python bench/dataset.py --items 300
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

SOURCE = ("https://raw.githubusercontent.com/openai/grade-school-math/"
          "master/grade_school_math/data/test.jsonl")

GOLD_RE = re.compile(r"####\s*([-+]?[\d,]*\.?\d+)")
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")

INSTRUCTION = ("Solve the problem. Think step by step, then give the final "
               "numeric answer on the last line as: #### <number>")


def gold_of(answer: str) -> str | None:
    m = GOLD_RE.search(answer)
    return m.group(1).replace(",", "") if m else None


def extract(text: str) -> str | None:
    """The model's final number.

    Prefer the '#### x' the prompt asked for; fall back to the last number in
    the reply, because a small model often reasons correctly and then forgets
    the format. Grading format compliance instead of arithmetic would make the
    weak model look worse than it is, and the whole benchmark is about how bad
    the weak model really is.
    """
    m = GOLD_RE.search(text)
    if m:
        return m.group(1).replace(",", "")
    numbers = NUMBER_RE.findall(text)
    return numbers[-1].replace(",", "") if numbers else None


def correct(reply: str, gold: str) -> bool:
    got = extract(reply)
    if got is None:
        return False
    try:
        return abs(float(got) - float(gold)) < 1e-4
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", type=int, default=300)
    ap.add_argument("--out", default="bench/runs/gsm8k/items.jsonl")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=60) as resp:
        lines = resp.read().decode("utf-8").splitlines()

    # the first N of the official test split: no cherry-picking, and anyone
    # can reproduce the exact selection
    written = 0
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for n, line in enumerate(lines):
            if written >= args.items:
                break
            row = json.loads(line)
            gold = gold_of(row["answer"])
            if gold is None:
                continue
            fh.write(json.dumps({
                "item": f"gsm8k_{n:04d}",
                "question": row["question"],
                "gold": gold,
                "instruction": INSTRUCTION,
            }, ensure_ascii=False) + "\n")
            written += 1

    print(f"wrote {written} items to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
