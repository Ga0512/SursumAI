#!/usr/bin/env python3
"""Score every routing policy against the same answer table.

This is step 4 of the benchmark and the only step that needs no GPU: once
`answers.jsonl` says which model got which item right, every routing policy is
just a rule replayed over that table. Adding a policy — or changing one — costs
nothing, which is the whole reason generation and routing are separated.

Two numbers per policy: how often it answered correctly, and how often it had
to wake the expensive model.

    python bench/score.py --run bench/runs/gsm8k-100
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The stage rule is imported, never reimplemented: a benchmark of a
# reimplementation measures the reimplementation.
from central.router import _stage_wants_strong  # noqa: E402


def _utf8_output() -> None:
    """Same reason as web/server.py: a cp1252 console kills the process on a
    middle dot, and the table is full of them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class Table:
    """Who answered what, and whether it was right."""

    def __init__(self, run: Path):
        items = load_jsonl(run / "items.jsonl")
        self.question = {r["item"]: r["question"] for r in items}
        self.items = [r["item"] for r in items]

        self.correct: dict[tuple[str, str], bool] = {}
        for r in load_jsonl(run / "answers.jsonl"):
            self.correct[(r["item"], r["model"])] = bool(r["correct"])

        self.verdict: dict[tuple[str, str], bool] = {}
        for r in load_jsonl(run / "verdicts.jsonl"):
            self.verdict[(r["item"], r["judge"])] = bool(r["escalate"])

        self.choice: dict[tuple[str, str], str] = {}
        for r in load_jsonl(run / "choices.jsonl"):
            self.choice[(r["item"], r["judge"])] = r["choice"]

        meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
        self.ladder: list[str] = meta["ladder"]       # weakest first
        self.judges: list[str] = meta["judges"]

    @property
    def weak(self) -> str:
        return self.ladder[0]

    @property
    def strong(self) -> str:
        return self.ladder[-1]

    def ok(self, item: str, model: str) -> bool:
        return self.correct.get((item, model), False)


def evaluate(t: Table, name: str, route) -> dict:
    """Run one policy over every item. `route(item) -> model that answers`."""
    hits = strong_calls = 0
    for item in t.items:
        served = route(item)
        hits += t.ok(item, served)
        strong_calls += served == t.strong
    n = len(t.items)
    return {
        "policy": name,
        "accuracy": hits / n,
        "strong_rate": strong_calls / n,
    }


def coin_accuracy(t: Table, rate: float) -> float:
    """What a coin flipped at this same escalation rate would score.

    Policies escalate at different rates, and accuracy rises with the rate no
    matter how the choice is made — so raw accuracy cannot be compared across
    rows. Comparing each policy against a coin at *its own* rate is what
    isolates the routing skill from the spending.
    """
    total = 0.0
    for item in t.items:
        total += rate * t.ok(item, t.strong) + (1 - rate) * t.ok(item, t.weak)
    return total / len(t.items)


def policies(t: Table) -> list[dict]:
    rows = [
        evaluate(t, f"only {t.weak}", lambda i: t.weak),
        evaluate(t, "stage (rules, no judge)",
                 lambda i: t.strong
                 if _stage_wants_strong([{"role": "user", "content": t.question[i]}])
                 else t.weak),
    ]

    for judge in t.judges:
        if any((i, judge) in t.verdict for i in t.items):
            rows.append(evaluate(
                t, f"escalation · judge {judge}",
                lambda i, j=judge: t.strong if t.verdict.get((i, j)) else t.weak))

    for judge in t.judges:
        if any((i, judge) in t.choice for i in t.items):
            rows.append(evaluate(
                t, f"classifier · judge {judge}",
                lambda i, j=judge: t.choice.get((i, j), t.weak)))

    order = {item: n for n, item in enumerate(t.items)}
    rows.append(evaluate(t, "round_robin",
                         lambda i: t.ladder[order[i] % len(t.ladder)]))

    # Escalate only when it actually helps: the ceiling for this ladder.
    rows.append(evaluate(
        t, "oracle",
        lambda i: t.strong if (not t.ok(i, t.weak) and t.ok(i, t.strong)) else t.weak))
    rows.append(evaluate(t, f"only {t.strong}", lambda i: t.strong))

    for r in rows:
        r["vs_coin"] = r["accuracy"] - coin_accuracy(t, r["strong_rate"])
    return rows


def judge_quality(t: Table) -> list[dict]:
    """Where each judge is wrong, and in which direction.

    Missing a bad answer costs the user a bad answer; escalating a good one
    costs compute. Averaging the two into one accuracy hides the trade.
    """
    out = []
    for judge in t.judges:
        tp = fp = tn = fn = 0
        for item in t.items:
            if (item, judge) not in t.verdict:
                break
            should = not t.ok(item, t.weak)      # ground truth: weak failed
            said = t.verdict[(item, judge)]
            if should and said:
                tp += 1
            elif should and not said:
                fn += 1
            elif not should and said:
                fp += 1
            else:
                tn += 1
        else:
            out.append({
                "judge": judge,
                "recall": tp / (tp + fn) if tp + fn else 0.0,
                "precision": tp / (tp + fp) if tp + fp else 0.0,
                "missed_bad": fn,
                "woke_strong_for_nothing": fp,
            })
    return out


def render(rows: list[dict], judges: list[dict], n: int) -> str:
    floor = next(r["accuracy"] for r in rows if r["policy"].startswith("only "))
    ceiling = next(r["accuracy"] for r in rows if r["policy"] == "oracle")
    span = ceiling - floor

    w = max(len(r["policy"]) for r in rows) + 2
    out = [f"{n} items\n",
           f"{'policy'.ljust(w)}{'correct':>9}{'wakes strong':>14}"
           f"{'headroom':>10}{'vs coin':>9}",
           "-" * (w + 42)]
    for r in rows:
        head = (r["accuracy"] - floor) / span if span > 0 else 0.0
        out.append(f"{r['policy'].ljust(w)}"
                   f"{r['accuracy']:>8.0%} "
                   f"{r['strong_rate']:>13.0%}"
                   f"{head:>10.0%}"
                   f"{r['vs_coin']:>+9.0%}")
    out += ["",
            "vs coin = accuracy minus a coin flipped at the same escalation rate.",
            "          At or below zero the policy is spending, not deciding."]

    if judges:
        out += ["", "judge quality (does it spot the weak model failing?)",
                f"{'judge'.ljust(w)}{'recall':>9}{'precision':>11}"
                f"{'missed':>9}{'wasted':>9}", "-" * (w + 38)]
        for j in judges:
            out.append(f"{j['judge'].ljust(w)}"
                       f"{j['recall']:>8.0%} "
                       f"{j['precision']:>10.0%}"
                       f"{j['missed_bad']:>9}"
                       f"{j['woke_strong_for_nothing']:>9}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="bench/runs/gsm8k-100", help="directory with the jsonl files")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    _utf8_output()

    run = Path(args.run)
    if not (run / "meta.json").exists():
        print(f"no run at {run} — generate one with bench/make_fake.py", file=sys.stderr)
        return 1

    t = Table(run)
    rows = policies(t)
    judges = judge_quality(t)

    if args.json:
        print(json.dumps({"policies": rows, "judges": judges,
                          "items": len(t.items)}, indent=2))
    else:
        print(render(rows, judges, len(t.items)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
