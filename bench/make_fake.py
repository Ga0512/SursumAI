#!/usr/bin/env python3
"""Generate a synthetic run so the output table can be judged before any GPU
time is spent.

Nothing here is a result. It exists to fix the shape of the report: if the
table is the wrong shape, that should be discovered now and not after four
models have been run over 300 items.

The world it invents is deliberately plausible rather than flattering:
bigger models are better but not strictly superset, harder items attract the
"strong" keywords, and a judge gets a noisy view of the truth that sharpens
with size.

    python bench/make_fake.py && python bench/score.py
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

LADDER = ["Qwen3-0.6B", "Qwen3-1.7B", "Qwen3-4B", "Qwen3-8B"]
JUDGES = ["Qwen3-0.6B", "Qwen3-1.7B", "Qwen3-4B"]

# accuracy each model reaches on this imagined task mix
SKILL = {"Qwen3-0.6B": 0.41, "Qwen3-1.7B": 0.55, "Qwen3-4B": 0.64, "Qwen3-8B": 0.69}

# how reliably a judge sees that an answer failed; small judges are noisy and
# lean towards approving whatever they are shown
JUDGE_NOISE = {"Qwen3-0.6B": 0.38, "Qwen3-1.7B": 0.22, "Qwen3-4B": 0.12}
JUDGE_LENIENCY = {"Qwen3-0.6B": 0.30, "Qwen3-1.7B": 0.15, "Qwen3-4B": 0.08}

EASY = ["what day comes after monday", "say hello in portuguese",
        "how many letters in the word bench", "capital of france",
        "is 10 greater than 3"]
HARD = ["explain why the derivative of sin is cos",
        "prove this theorem about even numbers",
        "solve the equation for x and show the calculus",
        "debug this python function and explain the traceback",
        "compare the two algorithms and analyze the complexity"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="bench/runs/fake")
    ap.add_argument("--items", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    items, answers, verdicts, choices = [], [], [], []

    for n in range(args.items):
        item = f"gsm8k_{n:04d}"
        difficulty = rng.random()
        # hard items are the ones that read like work, most of the time
        pool = HARD if (difficulty > 0.5) == (rng.random() < 0.75) else EASY
        items.append({"item": item, "question": rng.choice(pool),
                      "difficulty": round(difficulty, 3)})

        for model in LADDER:
            correct = difficulty < SKILL[model] + rng.gauss(0, 0.08)
            answers.append({"item": item, "model": model, "correct": bool(correct),
                            "tokens": rng.randint(80, 600),
                            "ms": rng.randint(300, 9000)})

        weak_failed = not next(a["correct"] for a in answers[-len(LADDER):])

        for judge in JUDGES:
            if rng.random() < JUDGE_NOISE[judge]:
                escalate = rng.random() < 0.5           # judge is guessing
            else:
                escalate = weak_failed
            if escalate and rng.random() < JUDGE_LENIENCY[judge]:
                escalate = False                        # small judges approve too readily
            verdicts.append({"item": item, "judge": judge,
                             "target": LADDER[0], "escalate": bool(escalate)})

            # classifier picks one of N from the question alone — the harder,
            # genuinely predictive task, so it is noisier than the binary call
            if rng.random() < JUDGE_NOISE[judge] * 1.6:
                choice = rng.choice(LADDER)
            else:
                choice = LADDER[-1] if difficulty > 0.5 else LADDER[0]
            choices.append({"item": item, "judge": judge, "choice": choice})

    def dump(name: str, rows: list[dict]) -> None:
        with (out / name).open("w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump("items.jsonl", items)
    dump("answers.jsonl", answers)
    dump("verdicts.jsonl", verdicts)
    dump("choices.jsonl", choices)
    (out / "meta.json").write_text(
        json.dumps({"ladder": LADDER, "judges": JUDGES, "synthetic": True,
                    "note": "invented data — fixes the report shape, proves nothing"},
                   indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {args.items} synthetic items to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
