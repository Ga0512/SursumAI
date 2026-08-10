# MVP — SursumAI Local (roadmap atual)

> Arquitetura do MVP: **3 processos separados**, mesma máquina.
> SursumAI = control plane de deploy de modelos open/custom (vLLM + llama-server),
> com preflight de ambiente, custo visível e multi-target futuro.

## Arquitetura do MVP

```
[Browser]
   │
   ▼
Web (3000) ──► Backend Central (8001) ──► Local Agent (8010)
   estático       db · auth · métricas ·       executor (vLLM docker / llama-server)
                  decisões                     roda na máquina, reporta de volta
   │
   └── deploys ocupam 9000-9099
```

- **Web (3000)**: front estático (`web/server.py`). Produção → CDN/deploy separado.
- **Backend Central (8001)**: dono dos dados (SQLite), **auth real (email+senha PBKDF2, sessões bearer)**, métricas, custo, decisões ("onde rodar"). `user_id` é o ponto em comum de todos os dados. Produção → SaaS hospedado.
- **Local Agent (8010)**: só executa (docker vLLM / llama-server docker+binário). Produção → agente instalado na máquina do cliente. Porta 8010 porque a 8000 pertence a outro projeto (`omnihunter-process-images`).
- Deploys ocupam **9000-9099** (BASE_PORT=9000) — evita colisão com serviços.

## Regra de separação (não-negociável)

| | Backend Central | Local Agent |
|---|---|---|
| DB (deploys, usuários, métricas) | ✓ | ✗ |
| Login/auth | ✓ | ✗ |
| Decidir onde rodar | ✓ | ✗ |
| Executar na máquina | ✗ | ✓ |
| Reportar status/logs/metrics | recebe | envia |

## Runtimes

| Runtime | Quando | Preflight |
|---|---|---|
| **vLLM** (docker, NVIDIA) | tem GPU NVIDIA | docker, GPU, imagem, modelo no HF |
| **llama-server** (GGUF) | GPU NVIDIA → docker; Mac/CPU → binário nativo pinado (sha256) | strategy, binário, modelo no HF, GGUF, VLM? |

Spec ganha campo `runtime: "vllm" | "llama"`.

## Fluxo do usuário

1. Abre `localhost:3000` → landing → **Sign in / Create account** (real, email+senha).
2. Dashboard → **+ New** → modal: provider → modelo, **Target: Local** (RunPod/AWS = "soon"), **Runtime** (vLLM ou llama-server), configs.
3. Deploy → fase `checking` → **preflight** valida ambiente (checkmark/erro claro).
4. `provisioning` → logs streaming → `healthy` com URL OpenAI.
5. Card com métricas + sparkline + status. Redeploy com edição. Destroy.
6. Tudo persistente (restart não apaga nada).

---

# Roadmap de Tasks

## FASE 0 — Fundação rápida (portas + persistência) ✅

### P-0.1 Portas: BASE_PORT dos deploys 8000 → 9000 ✅
- `agent/executor.py` `BASE_PORT = 9000` (range 9000-9099).
- Evita colisão com local-api (8010) e backend (8001).
**Done:** deploys sobem em 9000+, serviços ficam livres.

### P-0.2 Persistência ✅
- `start.sh` **não** apaga `sursumai.db`; restart preserva deploys/usuários/métricas.
- Reconcile no startup do central: deploys `healthy` sem container viram `failed`.
**Done:** restart não perde deploys.

## FASE 1 — Separação em 3 processos ✅

### P-1.1 Estrutura ✅
- `central/` = Backend Central (8001): db.py (schema users/sessions/deploys/metrics), auth, métricas, decisões, API.
- `agent/` = Local API (8010): execução docker (`agent/executor.py`), rotas de status/logs/metrics.
- `web/` = estático (3000), `web/server.py`.
**Done:** 3 processos sobem independentes, cada um na sua porta.

### P-1.2 Contrato central↔local (rotas HTTP do agent) ✅
- `POST /deploys` (spec) → inicia deploy (async).
- `GET /deploys/{id}/status` → running/healthy/endpoint.
- `GET /deploys/{id}/logs` → tail de logs.
- `GET /deploys/{id}/metrics` → snapshot vLLM.
- `POST /deploys/{id}/stop` → para.
- Todas exigem `X-Agent-Key` (env `AGENT_KEY`, default `dev-agent-key`).
**Done:** backend central chama o agent por HTTP (não conhece a máquina).

### P-1.3 Auth real + multi-tenant no Backend Central ✅
- **Auth real**: email+senha com PBKDF2 (200k iterações), sessões/tokens bearer (30d), logout.
- `user_id` como ponto em comum: deploys e métricas são por usuário (schema desde o início).
- Middleware `HTTPBearer` nas rotas protegidas (sem token → 401).
**Done:** deploys/métricas são por usuário; API exige token válido.

### P-1.4 UI aponta pro Backend Central ✅
- Web (3000) consome Backend Central (8001) com CORS (`localhost:3000`).
- Login/register real (email+senha), token no localStorage (`sg_token`), sign out.
- `web/app.js` `API = "http://localhost:8001"`.
**Done:** UI fala com 8001 autenticada; 8010 fala só com 8001.

## FASE 2 — Features do produto

### P-2.1 Spec: campo `runtime`
- `vllm | llama` com validação.
**Done:** runtime válido no spec e na UI (seletor no modal, payload, redeploy).

### P-2.2 Runtime llama-server
- **Estratégia híbrida**: GPU NVIDIA detectada (`nvidia-smi`) → docker `ghcr.io/ggml-org/llama.cpp:server` (único jeito de CUDA no Linux/WSL); sem NVIDIA → binário nativo do release oficial pinado (`b10327`, sha256 via GitHub API, extraído em `llama-bin/`) → Metal no Mac, CPU no resto.
- Download de GGUF via Hugging Face (`hf_hub_download`) com validação de magic bytes `GGUF` e sanitização de paths.
- **VLM automático**: detecta `mmproj-*.gguf` no repo e passa `--mmproj` (API OpenAI-compatible com `image_url`).
- Docker: usa `--runtime nvidia` só se `nvidia-smi` presente; senão roda CPU. Binário: `LD_LIBRARY_PATH` aponta para as libs bundled, pid file em `llama-bin/`/`sursumai-logs/`.
**Done:** deploy llama-server (LLM e VLM) fica healthy com URL OpenAI — via docker (GPU) e via binário nativo (Mac/CPU), ambos respondendo chat.

### P-2.3 Preflight (moat)
- Endpoint `POST /preflight` no agent, roteado por runtime.
- vLLM: docker, GPU NVIDIA, imagem (cached?), modelo existe no HF.
- llama: **strategy (docker ou binário)** + binário pronto (sha256 ok) + modelo existe no HF + GGUF encontrado + VLM/mmproj detectado.
- Fase `checking` no `_deploy_job` do central: salva `deploy.preflight`, aborta com `failed` se algum check falhar. Exibido na UI (✓/✗).
**Done:** problemas detectados ANTES de gastar GPU/tempo.

### P-2.4 Auth — remanescente
- (Auth principal já é P-1.3.) Se ficar pendente, fechar logout/refresh/sessão.
**Done:** fluxo de auth completo (login, logout, token válido).

## FASE 3 — Acabamento

### P-3.1 UI: runtime + cloud "soon" ✅
- Campo runtime no modal (vLLM / llama-server); Target RunPod/AWS desabilitado com "soon".
- Card mostra runtime no meta; status `checking` com checks preflight (✓/✗).
**Done:** escolha de runtime na UI; preflight visível no card; cloud marcado como roadmap.

### P-3.2 Cost tracking
- ~~`Transport.cost()` → estimativa $/h por deploy, mostrada no card.~~
- **Fora de escopo:** self-host local não tem cobrança real; estimativa fixa seria enganosa.
**Done:** removido do MVP (decisão 2026-08).

### P-3.3 Health-check real
- Detectar container/processo morto (não só checar `/v1/models`).
- `central/app.py`: `_reconcile_loop` a cada 10s marca `healthy` → `failed` se o processo/container sumiu no agent; reconcile no startup cobre `provisioning`/`redeploying` órfãos.
**Done:** deploy cai pra `failed` se o processo morre, mesmo sem restart do central.

## FASE 4 — Polimento

### P-4.1 Erros graciosos + validação end-to-end
- Mensagens claras em inglês na API e UI, sem stacktrace.
- Validar fluxo completo: login → preflight → deploy vLLM → deploy llama → métricas → redeploy → destroy → restart.
**Done:** MVP estável de ponta a ponta.

---

## Fora de escopo (MVP)
SaaS hospedado, multi-cloud real, billing, catálogo de modelos,
streaming de logs no dashboard (já existe), multi-tenant completo, agente on-prem.

## Ordem de execução
FASE 0 → FASE 1 (inclui auth real P-1.3) → FASE 2 (preflight é a prioridade) → FASE 3 → FASE 4.
