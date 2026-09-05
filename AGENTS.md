# Model-Serving-Framework

## REGRA CRÍTICA

**NUNCA executar comandos silenciosamente.** Se o usuário faz uma pergunta ou tira uma dúvida, RESPONDER com texto. Só executar comandos quando o usuário pedir explicitamente (ex: "roda", "builda", "executa", "instala").

## Princípio de produto (NÃO-NEGOCIÁVEL)

**O usuário-alvo é non-engineer.** A régua de simplicidade: *"clona, entra no WSL se estiver no Windows, e roda o `start.sh`. Pronto."* Deve ser tão fácil quanto subir um database. Isso guia TODAS as decisões de build do MVP:

- Uma única ação para subir tudo (nada de "crie venv", "pip install", "rode 3 processos", "sete keys").
- Zero conhecimento de infra exigido: GPU detectada sozinha (vLLM se NVIDIA, llama se não), portas/keys resolvidas e escondidas.
- UI como superfície: decisões técnicas (ex: runtime) ficam ocultas/automáticas, não expostas como toggle.
- Erros em linguagem humana, não stacktrace/`AgentError`.
- Instalador/scripts entregam tudo embutido e invisível (venv, deps, binário llama, docker).

## Estado atual

- SursumAI rodando como **3 processos separados** (arquitetura do ROADMAP.md): Web (3000), Backend Central (8001), Local Agent (8010).
- `qwen-vllm/`, `qwen-hf/`, `qwen-ollama/` e os entrypoints `api_server.py`/`handler.py`/Dockerfiles **não fazem mais parte do produto** (ficaram no histórico; `requirements.txt` ainda tem runpod para driver futuro).
- Testes com pytest em `tests/` (`python -m pytest`, 250 casos) + CI no GitHub Actions. Não há linter nem typecheck de Python.
- **Auth real**: email+senha com PBKDF2, sessões/tokens bearer. `user_id` é o ponto em comum de todos os dados (deploys/métricas).
- **API 100% default (não-negociável)**: não mexer no thinking do modelo — sem `enable_thinking:false` default. Reasoning/thinking é parte de `completion_tokens` e conta (verificado empiricamente: delta do gauge == `usage.completion_tokens`). Qwen3.5 pode gastar todo o contexto em reasoning (`content` vazio, `finish:length`) — aceito.
- **Detail modal (RunPod-style)** com 3 abas: Metrics (grid com sparkline; llama mostra "—" em requests/failed/KV/TTFT/latency pois llama.cpp não expõe), Test (playground dark com `reasoning_content` colapsado + content + meta de uso), Code (snippets Python/JS/curl). Card clicável + botão Details.
- **VLM no playground**: proxy de chat (`POST /deploys/{id}/chat`) aceita `messages: list[dict]` genérico — o tab Test manda `image_url` (base64 data URL) quando há imagem. Botão "Image" só habilita se o preflight tem check `vision.ok`.

## Segurança (não regredir)

- **Bind em `127.0.0.1` por padrão** nos 3 processos. Rede só via `SURSUMAI_BIND`
  (`start.sh`, `web/server.py`, CLI). Nunca voltar `0.0.0.0` como default.
- **`AGENT_KEY`** vem de `core/keys.py`: gerada aleatória no 1º run em
  `~/.sursumai/agent.key` (0600), compartilhada por central e agent. `AGENT_KEY`
  no ambiente ganha. Comparação sempre com `hmac.compare_digest`
  (`keys.key_matches`). O agent **recusa subir** com a chave de dev se não
  estiver em loopback.
- A checagem de chave do agent é um **middleware** (`_agent_key_gate`), não só a
  dependência `_require_key`: o FastAPI valida o corpo antes das dependências, e
  sem o middleware um chamador não autenticado recebia 422 em vez de 401.
- **Uma API key por deploy** (`Spec.api_key`, gerada em `POST /deploys`).
  **Nunca em argv**: o comando vai para o log do deploy e argv é legível em
  `/proc`. llama-server usa `--api-key-file` (0600, montado read-only no
  container); vLLM usa `-e VLLM_API_KEY`. O agent ainda confirma via
  `auth_enforced` que o endpoint recusa chamada sem chave. Toda chamada ao endpoint do
  deploy — playground, router, judge, health probe, scrape de métricas — manda
  esse bearer. Os specs são persistidos em `~/.sursumai/specs/` (0600) para
  sobreviverem a um restart do agent, senão as probes perdem a chave.
- **Tokens de sessão são guardados com sha256** (`sessions.token_hash`). A
  migração dropa a tabela antiga: todo mundo faz login de novo, uma vez.
- **Instalador em tag fixa + checksum**: `install.sh` instala `v<X.Y.Z>`, nunca
  `main`, e confere o sha256 publicado no release. O update (UI e CLI) busca o
  installer da própria tag.

## Portas e pools (não regredir)

- **Porta é alocada pelo central**, nunca derivada de hash: menor livre em
  9000-9099, `UNIQUE INDEX` parcial em `deploys.port`, preflight do agent
  confere se está livre na máquina. `core/ports.legacy_port()` só existe para
  deploys antigos (`port NULL`) continuarem respondendo.
- **Pools são de N modelos**: `_ladder()` (primeiro = barato, último = forte).
  `round_robin` e `classifier` usam todos; os outros usam as duas pontas.
  `weak_id`/`strong_id` são legado.
- **Métricas são podadas** (`Store.METRICS_KEEP`) — a tabela crescia sem fim.
- **Nada de `except Exception: pass`** — `log.debug` para o esperado,
  `log.exception` para o resto.
- CI em `.github/workflows/tests.yml`: pytest (3.10/3.12), shellcheck, node --check.

## Arquitetura (SursumAI)

```
web/server.py (3000, estático) ──► central/app.py (8001) ──► agent/app.py (8010)
                                         │                        │
                                    central/db.py           agent/executor.py
                                    (SQLite: users,          (docker run vLLM,
                                     sessions, deploys,       pull, logs, status)
                                     metrics)
```

- **Central (8001)**: dono do DB, auth, métricas, decisões. `central/app.py`, `central/db.py`, `central/auth.py`, `central/agent_client.py`.
- **Agent (8010)**: só executa. `agent/app.py`, `agent/executor.py` (vLLM), `agent/executor_llama.py` (llama-server). Roteia por `spec.runtime`. Guarda X-Agent-Key (default `dev-agent-key`, via env `AGENT_KEY`).
- **Web (3000)**: estático, `web/app.js` aponta para `http://localhost:8001`. Login real com token no localStorage (`sg_token`).
- vLLM via docker `vllm/vllm-openai:v0.21.0`. **llama-server é híbrido**: GPU NVIDIA detectada (`nvidia-smi`) → docker `ghcr.io/ggml-org/llama.cpp:server`; sem NVIDIA → binário nativo do release oficial pinado (`BIN_VERSION=b10327`, sha256 via GitHub API, em `llama-bin/`, ignorado pelo git). Portas de deploy **9000-9099** (evita colisão com serviços).
- **Preflight**: `POST /preflight` no agent; o central roda fase `checking` antes de provisionar e salva `deploy.preflight` (aborta se algum check falhar). vLLM rejeita repo GGUF-only (safetensors requerido).
- GGUF baixado para `llama-models/<org>--<model>/` (ignorado pelo git, magic bytes `GGUF` validados). VLM detectado automaticamente via `mmproj-*.gguf`.
- **Métricas**: `core/metrics.py` com scrape dual — regex `vllm:{name}{labels}` e `llamacpp:{name}` (sem labels); agent expõe `--metrics` no llama (docker e binário). `derive()` gera rates.
- **Health-check real**: central roda `_reconcile_loop` a cada 10s — deploy `healthy` cujo processo/container sumiu no agent vira `failed` (não só no startup).

## Comandos

```bash
bash start.sh            # sobe os 3 processos (persistente, NÃO apaga sursumai.db)
python -m pytest         # testes (deps: requirements-dev.txt)
python -m pytest tests/test_spec.py::test_defaults_are_valid   # um teste só
.venv/bin/python -m uvicorn agent.app:app --port 8010
.venv/bin/python -m uvicorn central.app:app --port 8001
.venv/bin/python web/server.py --port 3000
```

- Logs dos processos: `/tmp/opencode/{agent,central,web}.log`.
- Porta 8000 pertence a **outro projeto** (`omnihunter-process-images`) — NÃO usar 8000.
- `sursumai.db` + `sursumai-logs/` são ignorados pelo git (.gitignore).
- vLLM vem da imagem docker; **não** está em `requirements.txt` (fastapi, uvicorn, huggingface_hub, python-dotenv — todos pinados com `==`). `runpod` foi removido (não era usado).

## Peculiaridades

- SQLite com `check_same_thread=False` + WAL (precisa, central usa threads para `agent_client`).
- Reconcile contínuo no central: `_reconcile_loop` a cada 10s (healthy→failed se processo morre) + reconcile no startup (cobre provisioning/redeploying órfãos).
- Token bearer: usar `HTTPBearer` (FastAPI) — header é `Authorization`, não `X-Auth`.
- README/docs e mensagens de commit em português (pt-BR).
