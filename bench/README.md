# Router benchmark

> Is a small LLM enough to decide which LLM should answer?

Short answer from this run: **yes when it verifies an answer, no when it has to
guess.** A 1.7B judge reading a 0.6B's reply caught 71% of its mistakes with no
false alarms. Asked to pick a model from the question alone, a 0.6B judge did
worse than a coin flip.

Everything below was measured on one machine: a 6 GB NVIDIA GPU, under WSL,
with `llama-server` from SursumAI's own deployments. The raw data is committed,
so every table here can be rebuilt without a GPU:

```bash
python bench/score.py --run bench/runs/gsm8k-100      # 0.6B -> 1.7B
python bench/score.py --run bench/runs/gsm8k-100-8b   # 0.6B -> 8B
```

---

## What is being measured

A SursumAI **pool** puts two or more models behind one name. For every message
the router decides who answers — the cheap model, or the strong one. Different
modes decide differently (see the main [README](../README.md#the-router--a-team-of-models-behind-one-name)).
A router is good if it reaches the strong model's quality while calling the
strong model as rarely as possible.

So each routing policy gets two numbers:

| column | meaning |
|---|---|
| **correct** | how many of the 100 problems it answered right |
| **calls strong** | on how many it had to call the expensive model to answer |

and two reading aids that exist to stop wrong conclusions:

| column | meaning |
|---|---|
| **vs coin** | its accuracy minus that of a coin flipped **at the same escalation rate** |
| **headroom** | where it lands between "cheap model only" (0%) and the oracle (100%) |

**`vs coin` is the column that matters.** Accuracy rises on its own the more you
escalate, however badly you choose — a policy that escalates 60% of the time
beats one that escalates 20% even if both are guessing. Comparing raw accuracy
across rows compares budgets, not decisions. A policy at or below zero on
`vs coin` is spending, not deciding.

The **oracle** knows the answers and escalates only when it helps: when the
cheap model is wrong *and* the strong one is right. It is the ceiling for a
given pair of models.

### How it works

Generation and routing are separated, which is what makes this affordable:

1. **Every model answers every problem on its own.** No routing involved. Each
   answer is graded, which fills in a table of who got what right.
2. **Each judge reads each of the cheap model's answers** — the question and
   the reply, never the gold answer, exactly as in production — and says
   "fine" or "escalate". For the `classifier` mode it also picks a model from
   the question alone.
3. **Every policy is replayed over those two files.** No model runs in this
   step, so adding or changing a policy costs nothing.

Steps 1 and 2 use SursumAI's public API — the same deploy, chat and destroy
calls any client makes. The `stage` rule and the judge prompts are imported
from `central/router.py`, never copied: benchmarking a copy measures the copy.

### Why GSM8K

Grade-school maths word problems ([GSM8K](https://github.com/openai/grade-school-math),
the first 100 of the official test split, no selection). The model has to
reason over several lines, but the verdict is a number compared to a number.

**No LLM grades anything.** Using a model to decide whether a model answered
well is exactly the circularity this benchmark exists to avoid. That rules out
the parts of [RouterBench](https://arxiv.org/abs/2403.12031) labelled by GPT-4
(MBPP, MT-Bench, RAG), and its multiple-choice sets were left out too:
judging whether the letter "C" is a good answer is nothing like what a judge
does in practice.

---

## Results

### The models alone

| model | correct | median time per answer |
|---|---|---|
| Qwen3-0.6B | 69% | 6.0 s |
| Qwen3-1.7B | 86% | 9.8 s |
| Qwen3-8B | 93% | 88 s |

All GGUF, `temperature=0`, 4096-token budget (Qwen3 reasons before it answers,
and the reasoning counts). The 8B does not fit in 6 GB: `llama-server` put
4.2 GB of it on the GPU and ran the rest on the CPU, at 16 tokens/s.

### The judge

The judge reads the question and the 0.6B's reply and decides whether to
escalate. Of the 100 replies, 31 were wrong.

| judge | wrong replies caught | precision | missed | false alarms |
|---|---|---|---|---|
| Qwen3-0.6B | 48% (15 of 31) | 83% | 16 | 3 |
| Qwen3-1.7B | **71% (22 of 31)** | **100%** | 9 | **0** |

A bigger judge helps: going from 0.6B to 1.7B raised recall from 48% to 71%
and took false alarms to zero. Both judges are conservative — they rarely
escalate for nothing, and the misses are the cost.

### Routing 0.6B → 8B — where it pays

| policy | correct | calls 8B | vs coin | mean time per message |
|---|---|---|---|---|
| 0.6B only | 69% | 0% | — | 11.8 s |
| `escalation`, 0.6B judge | 80% | 18% | +7 | 33 s |
| **`escalation`, 1.7B judge** | **87%** | **22%** | **+13** | **39 s** |
| `round_robin` | 82% | 50% | +1 | — |
| oracle | 93% | 24% | +18 | — |
| 8B only | 93% | 100% | — | 107 s |

With a 1.7B judge the router reached **87% against 93% for the 8B alone, in
about a third of the time per message.** The time column adds everything a
routed message costs: the cheap answer, the judge call, and the strong answer
on the share that escalates (e.g. 11.8 + 3.5 + 22% × 107.1 ≈ 39 s).

With the default judge — the cheapest member of the pool, here the 0.6B
itself — it reached 80% in about 33 s.

### Routing 0.6B → 1.7B — where it does not

| policy | correct | calls 1.7B | vs coin |
|---|---|---|---|
| 0.6B only | 69% | 0% | — |
| `stage` (keyword rules, no judge) | 69% | 1% | 0 |
| `escalation`, 0.6B judge | 78% | 18% | +6 |
| **`escalation`, 1.7B judge** | **85%** | **22%** | **+12** |
| `classifier`, 0.6B judge | 77% | 76% | **−5** |
| `classifier`, 1.7B judge | 80% | 52% | +2 |
| `round_robin` | 77% | 50% | −1 |
| oracle | 88% | 19% | +16 |
| 1.7B only | 86% | 100% | — |

The routing decisions are just as good here (+12 over the coin), but they save
nothing: the 1.7B is only ~60% slower than the 0.6B, so the cheap answer plus a
judge call costs about as much as calling the 1.7B directly — roughly 11 s per
message either way. **A router pays when the strong model is much more
expensive than the cheap one, and only then.**

---

## What this answers

- **Verifying works; predicting does not.** In `escalation` the judge reads an
  answer that already exists. That is the best-scoring mode in both ladders.
  In `classifier` the judge must pick a model from the question alone, with
  nothing to inspect — a 0.6B judge did worse than a coin (−5), a 1.7B one
  roughly tied it (+2).
- **Keyword rules depend on the traffic.** `stage` fired on 1 problem in 100:
  maths word problems almost never say "math", "equation" or "prove". It costs
  nothing, and here it did nothing.
- **The judge's size matters.** The same 0.6B answers, judged by a 1.7B
  instead of a 0.6B, went from 78–80% to 85–87% with no false alarms.

## What it does not answer

- **The time figures assume every model stays loaded.** On this 6 GB card the
  8B alone takes 4.2 GB; the 0.6B and the 8B do not fit together, so a live
  router here would swap models in and out of VRAM on every escalation. That
  swap was not measured and could well eat the gain. The numbers hold on a card
  that keeps the whole pool resident.
- **100 problems.** Each accuracy carries roughly ±7 points of uncertainty.
  Differences of one or two points between rows mean nothing.
- **Maths only.** Nothing here speaks to code, conversation or vision.
- **Two-model ladders.** In the 0.6B → 1.7B run the 1.7B judge is also the
  strong model.
- **`advisor` and latching were not measured.** Both act on the *next* turn,
  and every GSM8K problem is a single turn.
- **Not pre-registered in git.** [`PLAN.md`](PLAN.md) (Portuguese) was written
  before the first run but committed with the results, so git does not prove
  the order. The plan called for 300 problems and four models; this run used
  100 problems and three models, because with the model still on the CPU (bug
  1 below) each problem took ~70 s and the full plan ran past a day.

---

## What the benchmark found in the product

Running one model for an hour straight surfaced bugs no test caught. All five
are fixed in v0.8.2.

1. **The NVIDIA + Docker path never used the GPU.** It ran
   `llama.cpp:server`, a CPU-only image: with `--gpus all` it logs "no usable
   GPU found" and the card sat at 0 MiB. Now `:server-cuda` with `-ngl auto`
   — **44 → 192 tokens/s** on the 0.6B. `auto` rather than a fixed count, so
   a model bigger than the card is split across GPU and CPU instead of failing
   to load; that is how the 8B above ran at all.
2. **`llama-server` was killed for lack of memory.** Its host-RAM prompt cache
   defaults to 8 GB, more than a stock WSL has in total. Under steady traffic
   it passed 5 GB and the OOM killer took it mid-request. Now capped at an
   eighth of RAM.
3. **A 180 s timeout that lied.** The central gave up after 180 s and reported
   "deploy unreachable" while the model was alive and thinking. Now 900 s
   (`SURSUMAI_CHAT_TIMEOUT`), and a timeout says the model is taking long.
4. **A just-freed port refused.** The preflight waited 8 s for the port of a
   deployment that had just been destroyed; Docker Desktop takes over 30 s to
   let go of it. Now up to 90 s (`SURSUMAI_PORT_WAIT`).
5. **The router's judge had 120 tokens.** Qwen3 reasons before it answers, and
   with 120 tokens the verdict could come back empty — which the router reads
   as "do not escalate". The router could quietly never route. Now 1024, the
   value measured here.

And a measurement trap worth knowing about: with a 1024-token budget, half the
0.6B's answers were cut off mid-reasoning and it appeared to score 44%. Given
room to finish, it scores 69%. **A budget that truncates measures the budget,
not the model.** The 1024-token run is kept in `runs/pilot-budget1024/`.

---

## Running it yourself

With SursumAI running and `sursumai login` done (deploying needs a session
token — an account API key is scoped to `/v1` and cannot create deployments):

```bash
python bench/dataset.py --items 100 --out bench/runs/mine/items.jsonl
python bench/run_models.py --run bench/runs/mine --models Qwen/Qwen3-0.6B-GGUF Qwen/Qwen3-1.7B-GGUF
python bench/run_judges.py --run bench/runs/mine --judges Qwen/Qwen3-0.6B-GGUF Qwen/Qwen3-1.7B-GGUF
python bench/score.py --run bench/runs/mine
```

Models run one at a time, so it fits a small GPU. Every step is resumable:
answers are written as they are produced, a rerun skips what is already there,
and a model server that stops responding is replaced and the run carries on.
An infrastructure error is never recorded as a wrong answer.

| file | what it does |
|---|---|
| `dataset.py` | fetches GSM8K, grades a reply by its final number |
| `run_models.py` | step 1 — every model answers every problem |
| `run_judges.py` | step 2 — every judge rules on the cheap model's answers |
| `score.py` | step 3 — replays every policy and prints the tables |
| `client.py` | deploy / chat / destroy through the public API |
| `run_all.sh` | the whole thing, unattended |
| `make_fake.py` | a synthetic run, to check the report's shape before spending GPU time |
| `probe_gpu.py` | is a deployment actually on the GPU? |
