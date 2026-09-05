# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` holds the same guidance for other agents — keep the two in sync when architecture or rules change.

## Regra crítica

**Nunca executar comandos silenciosamente.** Pergunta do usuário → responder com texto. Só rodar comandos quando pedido explicitamente ("roda", "builda", "instala").

README, docs e mensagens de commit em **português (pt-BR)**. O código, a API e a UI ficam em inglês.

## Princípio de produto (não-negociável)

O usuário-alvo é **non-engineer**. A régua: *"clona, entra no WSL se estiver no Windows, roda o `start.sh`. Pronto."* Isso decide o design:

- Uma única ação sobe tudo — nada de "crie venv", "rode 3 processos", "sete keys".
- Zero conhecimento de infra: GPU detectada sozinha (vLLM se NVIDIA + Docker, `llama-server` caso contrário), portas e keys resolvidas e escondidas.
- Decisões técnicas ficam automáticas na UI, não expostas como toggle.
- Erros em linguagem humana, nunca stacktrace ou `AgentError`.
- Instalador entrega tudo embutido e invisível (venv, deps, binário llama, docker).

## Comandos

```bash
bash start.sh   # sobe os 3 processos; persistente, NÃO apaga sursumai.db
bash setup.sh   # venv + deps (start.sh chama sozinho no primeiro run)

python -m pytest                       # toda a suíte
python -m pytest tests/test_spec.py    # um arquivo
python -m pytest -k relabel            # um teste (deps: requirements-dev.txt)

# processos individuais, para debug
.venv/bin/python -m uvicorn agent.app:app --port 8010
.venv/bin/python -m uvicorn central.app:app --port 8001
.venv/bin/python web/server.py --port 3000
```

- Logs dos serviços: `/tmp/opencode/{agent,central,web}.log`. Logs de deploy: `sursumai-logs/`.
- Testes em `tests/` (pytest, 250): `Spec`, portas, auth/hashes, API do central (auth + propriedade dos dados), router (ladder/modos/stage), `executor_llama` (resolução de modelo, GGUF, command lines), contrato agent↔central, streaming, e um e2e do router contra servidores HTTP/SSE de verdade (`test_router_e2e.py`, modelos falsos em sockets reais). CI em `.github/workflows/tests.yml` roda pytest (3.10 e 3.12), `bash -n` + shellcheck e `node --check`. Não há linter nem typecheck de Python.
- **Porta 8000 pertence a outro projeto** (`omnihunter-process-images`) — nunca usar.
- Ambiente é Linux/WSL (scripts assumem `bash`, `/tmp`, `setsid`). Rodar do WSL, não do PowerShell.

## Arquitetura

```
web/server.py (3000, estático + proxy /api) ──► central/app.py (8001) ──► agent/app.py (8010)
                                                     │                          │
                                              central/db.py              agent/executor.py       (vLLM via docker)
                                              (SQLite: users, sessions,  agent/executor_llama.py (llama-server)
                                               deploys, metrics, pools,
                                               router_sessions, router_log)
```

- **Central (8001)** — dono do DB, auth, métricas e decisões. Nunca executa processo de modelo; fala com o agent por `central/agent_client.py` (header `X-Agent-Key`).
- **Agent (8010)** — só executa. Escolhe o executor por `spec.runtime` (`vllm` | `llama`). Autentica com `X-Agent-Key` (ver *Auth e segredos*).
- **Web (3000)** — `http.server` estático com proxy `/api` para o central; `web/app.js` (vanilla, sem build) guarda o token em `localStorage` (`sg_token`).
- **`core/spec.py`** — `Spec` é o contrato central/agent: valida runtime, portas 9000-9099, gpu_memory_utilization, etc. Trafega como dict (`to_dict`/`from_dict`) em toda chamada de deploy.

### Ciclo de vida de um deploy

`checking` (preflight no agent — aborta se algum check falhar) → `provisioning` → `healthy` | `failed`; `redeploying` para re-subida. O central roda `_reconcile_loop` a cada 10s: deploy `healthy` cujo processo/container sumiu no agent vira `failed`. O reconcile de startup cobre órfãos em `provisioning`/`redeploying`.

### Runtimes

- **vLLM** — docker `vllm/vllm-openai:v0.21.0`, exige NVIDIA + Docker. Rejeita repo GGUF-only (precisa de safetensors).
- **llama-server (híbrido)** — com NVIDIA (`nvidia-smi`): docker `ghcr.io/ggml-org/llama.cpp:server`. Sem NVIDIA: binário nativo do release oficial **pinado** (`BIN_VERSION`, sha256 conferido via GitHub API) baixado em `llama-bin/`. Detecta `libcuda` para build CUDA nativa sem Docker.
- GGUF vai para `llama-models/<org>--<model>/`, magic bytes `GGUF` validados. VLM detectado por `mmproj-*.gguf`.
- `llama-bin/`, `llama-models/`, `sursumai.db*` e `sursumai-logs/` são ignorados pelo git.

### Router (pools)

`central/router.py`: um pool junta 2+ deploys e responde em `POST /v1/chat/completions` com `model="router"`. Modos: `escalation` (juiz LLM escala para o forte; latch após 2 escalações), `classifier` (juiz escolhe 1 entre N), `advisor` (juiz roda em background, vale no próximo turno), `stage` (regras por keyword, sem LLM), `round_robin`. Estado por conversa vem do `session_id` do cliente, expira em 1h, persistido em `router_sessions`; decisões em `router_log` (visíveis no modal do pool).

Todo outcome sai por `_served()` e carrega `served_model` — o campo `model` da resposta vira `"<pool> → <modelo> (<decisão>)"`, que é o que a UI mostra na tag da mensagem.

`pick_target()` decide **sem gerar** nos modos que conseguem (stage, round_robin, classifier, sessão latched); nesses casos o central faz streaming direto do deploy escolhido e só reescreve o campo `model` de cada chunk. `escalation`/`advisor` precisam ler uma primeira resposta antes de rotear, então a resposta já está pronta e é reenviada em fatias por `_replay_chunks` — fatias, nunca prefixos crescentes (esse era o bug: `text[:i+80]` fazia o cliente renderizar a resposta repetida).

### Portas de deploy

`core/ports.py` é a autoridade. O **central aloca** (menor porta livre em
9000-9099, sob `_port_lock`, com `UNIQUE INDEX` parcial em `deploys.port` como
palavra final) e grava em `spec.port`; o agent só usa o que recebe. O preflight
do agent checa se a porta está realmente livre na máquina.

`ports.legacy_port()` (o antigo `sha256(deploy_id) % 100`) existe **só** para
deploys criados antes da alocação continuarem respondendo onde estão ouvindo —
era exatamente o bug: 100 slots por hash colidem em ~50% já no 12º deploy, e
ninguém checava. Deploy legado (`port NULL`) ganha porta de verdade no próximo
redeploy. Nunca voltar a derivar porta de hash para deploy novo.

### Pools de N modelos

`_ladder()` é a ordem do pool (`pool_models`, filtrada por quem tem endpoint):
primeiro = mais barato, último = mais forte. `escalation`, `advisor` e `stage`
usam as duas pontas; `round_robin` percorre todos; `classifier` escolhe entre
todos. `weak_id`/`strong_id` na tabela `pools` são legado — leia pela ladder,
não por eles.

### Métricas

Retenção: `save_metrics` poda para os últimos `Store.METRICS_KEEP` snapshots por deploy (~3h a 10s) e há índice em `(deploy_id, ts DESC)` — sem isso a tabela crescia para sempre e cada poll do dashboard ficava mais lento.

`core/metrics.py` faz scrape dual do endpoint Prometheus: regex `vllm:{name}{labels}` e `llamacpp:{name}` (sem labels); `derive()` gera as rates. O agent passa `--metrics` ao llama (docker e binário). llama.cpp não expõe requests/failed/KV/TTFT/latency — a UI mostra "—" nesses campos.

### Auth e segredos

Email + senha com PBKDF2 (`central/auth.py`), sessões com token bearer via `HTTPBearer` (header `Authorization`, **não** `X-Auth`). `user_id` é o ponto em comum de todos os dados (deploys, pools, métricas). O token é guardado **hasheado** (`sessions.token_hash`, sha256) — a migração dropa a tabela antiga e todo mundo loga de novo uma vez.

Três segredos, três donos:

- **`AGENT_KEY`** (`core/keys.py`) protege o agent. Gerada aleatória no 1º run em `~/.sursumai/agent.key` (0600); `AGENT_KEY` no ambiente ganha. Comparação sempre por `hmac.compare_digest`. O agent recusa subir com a chave de dev fora de loopback, e a checagem é um **middleware** — como dependência ela rodava depois da validação do corpo, devolvendo 422 antes de 401.
- **`Spec.api_key`** protege cada deploy: gerada em `POST /deploys`. Toda chamada ao endpoint do deploy (playground, router, judge, health probe, métricas) manda esse bearer; os specs ficam em `~/.sursumai/specs/` (0600) para sobreviver a um restart do agent. **Nunca colocar a chave em argv** — `" ".join(cmd)` vai para o log do deploy, que o README manda o usuário abrir quando algo falha, e argv é legível em `/proc/<PID>/cmdline`. llama-server lê de `--api-key-file` (arquivo 0600 em `~/.sursumai/deploys/`, montado read-only no container); vLLM não tem essa opção e recebe por `-e VLLM_API_KEY` (flag `-e` sem valor: o docker herda do processo chamador). O `-e`/`-v` tem que vir **antes** da imagem — depois dela tudo é argumento do servidor do modelo.
- **`auth_enforced`** no status do agent: depois de saudável, o agent faz uma sonda *sem* a chave e confirma que leva 401/403. Se um runtime ignorasse a chave, ele subiria aberto e a sonda autenticada passaria igual — controle de segurança que falha em silêncio é pior que nenhum. O central loga `error` se isso acontecer.
- **Token de sessão** protege a API do central, como acima.

### Bind

Os 3 processos escutam em `127.0.0.1`. Exposição na rede é opt-in por `SURSUMAI_BIND` (respeitado por `start.sh`, `web/server.py` e o CLI). Nunca voltar `0.0.0.0` como default.

## Peculiaridades

- SQLite com `check_same_thread=False` + WAL — necessário porque o central usa threads para o `agent_client`.
- Migrações em `central/db.py` são `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`; adicionar coluna nova segue esse padrão (não recriar tabela). Exceção deliberada: `sessions` foi recriada porque token em texto puro não migra.
- Ciclo de vida das apps é `lifespan` (não `@app.on_event`, depreciado na versão pinada do FastAPI); o `lifespan` do central cancela os loops de background ao encerrar.
- **Não engolir exceção.** `except Exception: pass` some com bug real (o `_metrics_loop` fazia isso e falha de scrape ficava idêntica a modelo ocioso). Use `log.debug` para o esperado e `log.exception` para o resto.
- **API 100% default**: não mexer no thinking do modelo — sem `enable_thinking:false`. Reasoning conta em `completion_tokens` (verificado empiricamente). Um modelo pode gastar todo o contexto em reasoning (`content` vazio, `finish:length`) — aceito.
- O proxy de chat (`POST /deploys/{id}/chat`) aceita `messages: list[dict]` genérico, para o playground mandar `image_url` (data URL base64) em modelos de visão. O botão "Image" só habilita se o preflight trouxe `vision.ok`.
- `requirements.txt` é mínimo (fastapi, uvicorn, python-dotenv, huggingface_hub, runpod); vLLM vem da imagem docker, nunca do pip. `runpod` está lá para um driver futuro.
- `VERSION` é lido por `/meta/update` e por `sursumai update`. O "latest" vem do **release mais novo** na API do GitHub, não de `raw/main/VERSION`; o update baixa o `install.sh` da própria tag e roda com `SURSUMAI_VERSION=v<X.Y.Z>`. Ao lançar: bump em `VERSION`, no `SURSUMAI_VERSION` default do `install.sh` e no comando de instalação do README, e publique o `SHA256SUMS` do tarball no release (o instalador aborta se o hash não bater, e só avisa se não houver hash publicado).
