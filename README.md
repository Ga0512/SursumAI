# SursumAI — Self-Hosting Models

[![tests](https://github.com/Ga0512/SursumAI/actions/workflows/tests.yml/badge.svg)](https://github.com/Ga0512/SursumAI/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> Your model. Your machine. Your URL.

SursumAI runs **open-source AI models on your own machine** and gives you an
OpenAI-compatible URL to use — all through a web interface. No terminal,
Docker, Python, or GPU knowledge needed to get started.

Pick a model, click **Deploy**, and any OpenAI-compatible app can talk to it.

---

## Quick start

```bash
# 1. install (WSL on Windows, Terminal on Linux/macOS)
curl -fsSL https://github.com/Ga0512/SursumAI/raw/v0.8.0/install.sh | bash

# 2. the browser opens at http://localhost:3000 — create an account,
#    click "+ New", pick a model, click Deploy

# 3. create an API key under "API keys", then use it from anywhere:
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="sk-sursum-...")

resp = client.chat.completions.create(
    model="Qwen/Qwen3-0.6B-GGUF",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

That's the whole product: one URL, one key, and the model name.

---

## Screenshots

| | |
|---|---|
| ![Landing](docs/screenshots/01-landing.png) | ![Dashboard](docs/screenshots/02-dashboard.png) |
| **Landing** — one-click self-hosting | **Dashboard** — your deployments, health and metrics |
| ![Detail metrics](docs/screenshots/03-detail-metrics.png) | ![Playground](docs/screenshots/04-playground.png) |
| **Details** — metrics, test and code snippets | **Playground** — chat with the model (vision supported) |
| ![Chat](docs/screenshots/05-chat.png) | ![Pool modal](docs/screenshots/06-pool-modal.png) |
| **Chat** — the router picks the best model per message | **Pool** — team up 2+ models; the router decides between them |

---

## Requirements

| System | What you need |
|---|---|
| **Windows** | [WSL](https://learn.microsoft.com/en-us/windows/wsl/install) installed and open (Ubuntu recommended) |
| **Linux** | A terminal — nothing else |
| **macOS** | A terminal — nothing else |

> **NVIDIA GPU?** Even better: SursumAI detects it automatically and uses
> **vLLM** for maximum performance (requires [Docker Desktop](https://www.docker.com/products/docker-desktop/)).
> Without an NVIDIA GPU it uses `llama-server`, which runs on any machine —
> including vision models (VLM). You never have to decide.

## Install

Open a terminal (WSL on Windows, Terminal on Linux/macOS) and run:

```bash
curl -fsSL https://github.com/Ga0512/SursumAI/raw/v0.8.0/install.sh | bash
```

The installer downloads a **released tag** — never a moving branch — and
verifies the tarball against the sha256 published with that release before it
writes a single file.

Then it does the rest automatically:

- downloads SursumAI (no git needed)
- sets up Python and dependencies in a private virtualenv
- puts the `sursumai` command on your PATH
- adds a desktop icon (plus a Windows desktop shortcut if you're on WSL)
- optionally installs Docker, so your NVIDIA GPU can be used
- starts the app and opens `http://localhost:3000`

> Working from a clone instead?
> `git clone https://github.com/Ga0512/SursumAI.git && bash start.sh` works too.

## Everyday use

```bash
sursumai          # start everything and open the browser
sursumai status   # are the services running?
sursumai restart  # stop and start the services
sursumai stop     # stop the services (running model deployments stay up)
sursumai update   # update to the latest release
```

Or double-click the **SursumAI** icon in your app menu / Windows desktop.

---

## How it works

1. **Create an account** (email + password). Everything you create belongs to it.
2. **Deploy a model** — click **+ New** and pick one from the providers (Qwen,
   Kimi, DeepSeek, Muse-Glimmer, Mistral, Bonsai).
3. SursumAI **checks your machine**, downloads the model and starts it. The card
   moves through `checking → provisioning → healthy` (or `failed`, with a
   plain-language reason in the logs).
4. **Use it** from any OpenAI-compatible client. Ready-made Python / JavaScript /
   curl snippets are behind the **Details** button of each deployment.

### What SursumAI decides for you

- **Runtime** — vLLM when you have an NVIDIA GPU + Docker, `llama-server`
  otherwise. The selector is visible in the modal, but you never have to touch it.
- **Model format** — the provider list shows **safetensors** models (vLLM) and
  **GGUF** models (`llama-server`), already filtered by the chosen runtime.
- **Ports and internal keys** — allocated and hidden automatically.
- **Vision** — if the model accepts images (Qwen-VL, Bonsai, …), the **Test**
  tab grows an **Image** button so you can send a photo with your text.

### vLLM vs llama-server

| | vLLM | llama-server |
|---|---|---|
| **Best for** | Production / real traffic — external clients, high concurrency | One person or a small team on one machine |
| **Performance** | Highest throughput, production-grade batching | Lower throughput, but starts fast and runs anywhere |
| **Requires** | NVIDIA GPU + Docker | Nothing (CPU works; GPU optional) |
| **Typical use** | Serving a model as the API behind your product | Personal assistant, experiments, private team models |

> **NVIDIA GPU but no Docker?** SursumAI detects `libcuda` and runs a native
> CUDA build of `llama-server` on your GPU — no Vulkan, no Docker required.

---

## Using it from your code

There is **one base URL** — `http://localhost:8001/v1` — and the `model` field
decides who answers. SursumAI resolves it in this order:

| You send `model=` | You get |
|---|---|
| `"router"` or `"auto"` | Your default pool, routed |
| a deployment id (`d_a1b2…`) | That exact deployment |
| a pool id or pool name | That pool, routed |
| a model name (`Qwen/Qwen3-0.6B-GGUF`) | The deployment running it |

An unknown name returns **404** listing what does exist; a model that isn't
ready yet returns **422** with its current status. `GET /v1/models` lists
everything you can address: your deployments, your pools, and `router`.

Streaming (`stream=True`) works everywhere, including through the router.

**Authentication.** Create a key under **API keys** in the web UI, or run
`sursumai keys --create "my laptop"`. One key works for every model and pool in
your account.

## The Router — a team of models behind one name

A single model is always a compromise: the small one is cheap and fast but
gives up on hard questions; the big one is right more often but you pay for it
on *every* message, including "hi".

The router removes the choice. You put 2+ deployments into a **pool** and each
message goes to the model that should handle it. Your client keeps sending one
name — `"router"` — and never knows the difference.

### 1. Build the pool

In the web UI: **Pools → + New pool**, name it, and add the deployments in
order. **That order is the whole configuration** — first is the cheapest model,
last is the strongest. The list is the *ladder*, and every mode reads it the
same way.

```
my-pool:  Qwen3-0.6B  →  Qwen3-4B  →  Qwen3-8B
          ^ cheapest                   ^ strongest
```

A pool needs at least 2 members with a healthy endpoint. If one dies, the
router keeps working with the rest; if fewer than 2 survive you get a plain
error naming the pool.

### 2. Call it

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="sk-sursum-...")

resp = client.chat.completions.create(
    model="router",                       # or the pool's name: "my-pool"
    messages=[{"role": "user", "content": "hi"}],
)

print(resp.choices[0].message.content)
print(resp.model)   # "my-pool → Qwen/Qwen3-0.6B-GGUF (weak_ok)"
```

`model="router"` uses your first pool. To target a specific one, send its name.
Streaming (`stream=True`) works too, and every chunk carries the same label.

### 3. Read the answer

The `model` field of the response is rewritten to tell you exactly what
happened:

```
my-pool → Qwen/Qwen3-8B-GGUF (escalated)
└ pool     └ who actually answered   └ why
```

The reasons you'll see:

| Decision | Meaning |
|---|---|
| `weak_ok` | The cheap model answered and the judge accepted it |
| `weak_bad` | The cheap model came back empty or errored — served anyway, counted as a strike |
| `escalated` | Escalated to the strongest model for this message |
| `latched` | This conversation is now pinned to the strong model |
| `advisor` | Served instantly by the cheap model; the judge is still deliberating |
| `weak` / `strong` | `stage` mode picked by keyword rules |
| `classifier` | A judge model picked this one out of the pool |
| `round_robin[2/3]` | Second of three, taking turns |

Every decision is also written to the pool's log — open the pool in the UI to
see the history of who answered what and why.

### The five modes

Pick the mode when you create the pool.

#### `escalation` (default)

The cheap model answers. A **judge** — a small LLM call — reads the question and
that answer and decides whether it's good enough. If not, the strong model
answers instead. On the **second rejection in the same conversation** the
session *latches*: the router stops second-guessing and sends everything straight to the
strong model, because a conversation that needed the big model twice is a hard
conversation.

```
turn 1  "hi"                      → 0.6B answers, judge accepts it        strike 0
turn 2  "prove √2 is irrational"  → 0.6B answers, judge rejects it        strike 1
turn 3  "now generalize to √n"    → rejected again → 8B answers          strike 2 → latch
turn 4  "and for cube roots?"     → straight to 8B, no judge call         (latched)
```

Cost: one extra small judge call per message, until it latches.
**Use it when** quality matters and you'd rather pay a little to avoid a bad
answer.

#### `advisor` — same idea, zero added latency

Identical to `escalation`, except the judge runs **in the background** after
your answer has already been sent. The verdict applies to the *next* turn. You
never wait on the judge; the cost is that the router is always one message
behind.

**Use it when** latency is what you're protecting — interactive chat, an app
with a typing indicator.

#### `classifier` — pick the right specialist

A judge model reads the request, sees the whole pool with model names, and picks
the **single best one** for it. No cheapest-first assumption, no escalation
ladder — it just chooses. This is the NVIDIA-style `llm_classifier` approach.

```
"write a regex for emails"    → the coding model
"describe this photo"         → the vision model
"what time is it in Tokyo?"   → the tiny one
```

Cost: one judge call per message. **Use it when** your pool holds *specialists*
rather than sizes of the same thing.

#### `stage` — rules, no LLM, no latency

Keyword rules on the last user message. Hit a signal for code, math, reasoning
or long-form work and it goes to the strongest model; otherwise the cheapest one
answers. The signal list is **bilingual (English and Portuguese)** and accents
are stripped, so `matemática`, `matematica` and `math` all trigger.

```
"oi, tudo bem?"                     → cheapest
"explique a derivada de sin(x)"     → strongest  ("explique", "derivada")
"debug this traceback"              → strongest  ("debug", "traceback")
```

Cost: **nothing**. No extra call, no added milliseconds.
**Use it when** you want routing for free and your traffic is predictable.

#### `round_robin` — take turns

Each message goes to the next model in the pool, cycling through all N. No
judging, no ladder. **Use it for** spreading load across models, or for A/B
comparing them on real traffic.

### Session memory

Escalation streaks and latching are **per conversation**. To get them, send a
stable `session_id`:

```python
resp = client.chat.completions.create(
    model="router",
    messages=history,
    extra_body={"session_id": "my-app:user-42"},
)
```

Any stable string works — a user id, a thread id, `harness:my-pipeline`. State
expires after **1 hour** of inactivity and is stored server-side, so there's no
bookkeeping on your side. Without a `session_id`, routing still works; it just
treats every message as a fresh conversation, so `escalation` and `advisor`
never latch.

### The judge

The judge is just another model in your account. By default it's the **cheapest
member of the pool** (judging is a short, cheap call); you can point it at a
different deployment when you create the pool. It's asked for strict JSON and
**fails open** — if the judge is unreachable or answers garbage, the router
serves the cheap model's reply rather than erroring out.

### How the streaming works

For the modes that can decide *before* generating — `stage`, `round_robin`,
`classifier`, and any latched session — the router picks the target first and
then streams the model's tokens straight through, only rewriting the `model`
field on each chunk. You get real token-by-token streaming with no penalty.

`escalation` and `advisor` have to read a complete answer before they can route,
so by the time the decision exists the text is already done; it's then sent to
you in chunks. Same API, same shape — the tokens just aren't arriving live.

---

## Model providers

| Provider | Safetensors (vLLM) | GGUF (llama-server) |
|---|---|---|
| **Qwen** | Qwen3.6, Qwen3-VL… | Qwen3 30B MoE, 8B, VL-8B, 4B, 1.7B, 0.6B |
| **Kimi** | Kimi-K3, K2.6, K2.5, VL… | Kimi-K2 |
| **DeepSeek** | DeepSeek-V4… | R1 Distill 8B, 1.5B |
| **Muse-Glimmer** | Muse Glimmer 30B | Muse Glimmer 30B |
| **Mistral** | Mistral Small/Medium/Large | Mistral Small 24B |
| **Bonsai** | — | Bonsai 8B, 4B, 1.7B, 27B (1-bit) |

---

## CLI

Everything the web UI does, a terminal can do too:

```bash
sursumai login <email>                  # store your token (prompts for password)
sursumai whoami                         # who am I logged in as?
sursumai list                           # table of deployments
sursumai pools                          # table of pools
sursumai deploy <org/model>             # GGUF names auto-select llama-server
sursumai chat <id> "hello"              # one message to a deployment…
sursumai chat router "hello"            # …or to the router
sursumai logs <id> --follow             # tail a deployment's log live
sursumai destroy <id> --yes             # remove a deployment
sursumai keys                           # list account API keys
sursumai keys --create "my laptop"      # mint one (shown in plaintext once)
sursumai keys --revoke <id>             # revoke one
```

Deployment ids can be abbreviated to any unambiguous prefix. Add `--json` to
`status`, `list`, `pools` and `whoami` for machine-readable output. Every
command exits non-zero on failure, so it's safe to use in scripts.

---

## Docker (optional, recommended for GPU)

vLLM runs inside Docker. The installer can set it up for you, or do it manually:

1. Install **Docker Desktop**: https://www.docker.com/products/docker-desktop/
2. On Windows, open Docker settings and enable WSL integration.
3. Run `sursumai` again.

**No Docker?** SursumAI still works with the native `llama-server`. If you have
an NVIDIA GPU but no Docker, deployments fall back to CPU and say so — install
Docker later to unlock the GPU.

## Troubleshooting

| Symptom | What to do |
|---|---|
| **"Could not reach server"** | The services aren't running. Run `sursumai`. |
| **Deploy stuck in `failed`** | Open **Logs** on the deployment card — the message explains the problem in plain language. |
| **Model not showing up** | Some Hugging Face models require accepting a license. Use one of the listed models; they're already cleared. |
| **Port already in use** | The preflight names the port and the range. A deployment takes the lowest free port in 9000–9099. |

Service logs live in `/tmp/opencode/{agent,central,web}.log`; deployment logs in
`sursumai-logs/`.

## Security

SursumAI is a local app and is built to stay that way.

- **Loopback by default.** The three services listen on `127.0.0.1`. To reach
  them from another machine you have to opt in explicitly, with
  `SURSUMAI_BIND=0.0.0.0`.
- **A private agent key.** On first run a random key is generated in
  `~/.sursumai/agent.key` (readable only by you) and shared by the central and
  the agent. If you bind to the network while still on the built-in development
  key, the agent refuses to start.
- **Account API keys.** One key works for every model and pool you own — the
  same shape as an OpenAI or Anthropic key. Create as many as you like (one per
  machine or project) and revoke any of them. They're stored hashed; the
  plaintext is shown once, at creation. A key can call `/v1` only — it can never
  create or destroy deployments, so a key leaked from a script can't take your
  account apart.
- **Each model server is locked too.** Every deployment gets its own internal
  key, so nothing else on the machine can reach the model port directly. You
  never see or handle it, and it never appears in a log or in `ps`.
- **The lock is verified, not assumed.** Once a deployment is healthy, SursumAI
  probes it *without* the key and confirms it gets rejected. A security control
  that fails silently is worse than none.
- **Session tokens are stored hashed.** A copy of the database hands out no
  working logins. (Upgrading from an older version logs everyone out once.)

## Ports

| Service | Port |
|---|---|
| Web (UI) | 3000 |
| Central backend | 8001 |
| Local agent | 8010 |
| Deployments | 9000–9099, one per deployment |

A deployment is given the lowest free port in that range, and the preflight
fails with a plain message if something else on the machine is already using
it. The range holds 100 deployments at a time.

## License

MIT — see [LICENSE](LICENSE).
