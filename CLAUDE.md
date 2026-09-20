# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` holds the same guidance for other agents — keep the two in sync when architecture or rules change.

## Estado atual — leia antes de mexer (2026-09-20)

**Há trabalho pela metade.** O produto tem duas edições e, neste momento, **dois mecanismos de liberação do Pro convivem na árvore**. Isso é transitório e precisa terminar em um só.

### Os dois repositórios

| | Caminho local | Remote | O que tem |
|---|---|---|---|
| **Público** (Free, MIT) | `HDSeagate/Projects/Model-Serving-Framework` | `origin` = `Ga0512/SursumAI` | o app inteiro, menos as máquinas por SSH |
| **Pro** (privado) | `HDSeagate/Projects/SursumAI-Pro` | `origin` = `sursumai/SursumAI-Pro`, `upstream` = público | o público + `central/machines.py`, `central/tunnel.py`, aba Machines, `infra/stripe-worker/`, `PRO.md` |

O Pro acompanha o público por `git fetch upstream && git merge upstream/main`. `remote.upstream.tagOpt = --no-tags` está configurado: buscar o público trazia as tags dele e o release do Pro achava que a versão já existia.

Release: público = `bash release.sh` (tag + tarball + `SHA256SUMS`). Pro = `bash release_pro.sh` (tag + release sem assets; todo comando `gh` leva `--repo`, senão ele publica no repositório errado). **Rodar do WSL** com `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME`/e-mail no ambiente — o git do WSL não tem identidade configurada.

### Os dois mecanismos (é aqui que está a confusão)

1. **Token do GitHub — implementado e lançado na v0.9.0.** `core/edition.py`, `~/.sursumai/edition.json`, instalador escolhe o repositório pelo token. Testado instalando como comprador.
2. **Chave de licença — o que o usuário decidiu adotar, feito pela metade.** `core/ed25519.py` (verificação pura, vetores do RFC 8032) e `core/license.py` (`SURSUM-<payload>.<assinatura>`, offline, `~/.sursumai/license.json` 0600), endpoints `GET/POST/DELETE /meta/license`, e a tela "Upgrade to Pro" em `web/`. 31 testes passando.

**O que falta para fechar o 2 e apagar o 1:**

- **O repositório Pro está com um merge pela metade** (`git status` mostra `UU web/app.js`; o `web/style.css` já foi resolvido mas não foi `git add`). Resolver mantendo os dois lados (o conflito é só "ambos acrescentaram bloco no fim").
- Travar as máquinas atrás da licença **no backend**, não só escondendo a aba: era um `Depends(_pro)` com HTTP 402 em todas as rotas `/machines` e no `POST /deploys` com `machine_id`. Esconder a aba esconde um botão; a API é quem decide.
- `web/`: a aba Machines só aparece com licença (`onLicenseChanged()` existe no público como gancho vazio, o Pro sobrescreve).
- Worker: em vez de convidar no GitHub, conferir a sessão da Stripe e **entregar a chave assinada** na página de sucesso (`?session_id={CHECKOUT_SESSION_ID}`). A chave privada Ed25519 vive só como secret no Cloudflare; a metade pública vai em `core/license.py` (`PUBLIC_KEY_HEX`, hoje vazio) e o `release.sh` deve recusar publicar com ela vazia.
- Decidir onde o código do Pro mora quando o build for único. O usuário disse: *"o código baixado não é o do GitHub aberto"* — ou seja, fonte no repositório privado, e o tarball distribuído (com o Pro dentro) publicado como asset do release público.
- Depois disso: apagar `core/edition.py`, o caminho de token no `install.sh` e o `PRO.md` de convite.

### O que está pronto e verificado no Pro

Máquinas por SSH, testado de ponta a ponta **numa GPU de verdade** (pod RunPod, RTX PRO 4500): adicionar máquina (instala sozinho, ~20 s), deploy remoto com GPU, chat pela `/v1` atravessando o túnel (137 tok/s no Qwen3-8B), métricas, reinício do central, "reboot" do servidor, remoção. Dois bugs estruturais achados **só porque foi numa máquina real** e já corrigidos:

- **Nunca guardar no banco a ponta local de um túnel.** As portas locais são redistribuídas a cada start: depois de um restart o endereço salvo apontava para o túnel do agent (todo chat dava 401) e, com duas máquinas, apontaria para o modelo de outra máquina respondendo como se estivesse certo. O banco guarda o endereço que o agent reporta; a ponta local é resolvida a cada leitura (`db.ENDPOINT_RESOLVER` → `machines._resolve_endpoint`), e **ler um deploy nunca abre túnel** (o dashboard lê tudo a cada 5 s).
- **Um túnel só conta como aberto quando a porta local aceita conexão** (`Tunnel.ready`): o `ssh` existe alguns segundos antes de encaminhar, e nesse intervalo o chat batia em porta fechada.

### O que falta no produto (fora a licença)

Limites por API key e log de auditoria (a landing vende os dois e não existem), pool com modelos em máquinas diferentes (nunca testado), screenshots com a aba Machines, e o Payment Link no botão do site (`PRO_BUY_URL` em `web/app.js`, hoje vazio → o botão diz "Coming soon").

Do lado do usuário: produto e Payment Link na Stripe, Worker no Cloudflare, e uma compra de teste com o cartão 4242. Ele não vai usar domínio no começo (`*.workers.dev` e `*.pages.dev`).

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
- Testes em `tests/` (pytest, 266 funções / ~349 casos com parametrize): `Spec`, portas, auth/hashes, API do central (auth + propriedade dos dados), router (ladder/modos/stage), `executor_llama` (resolução de modelo, GGUF, command lines), contrato agent↔central, streaming, e um e2e do router contra servidores HTTP/SSE de verdade (`test_router_e2e.py`, modelos falsos em sockets reais). CI em `.github/workflows/tests.yml` roda pytest (3.10 e 3.12), `bash -n` + shellcheck e `node --check`. Não há linter nem typecheck de Python.
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
- **Site público (`site/`)** — landing, preços (Free / Pro $99, pagamento único) e instalação, hospedado à parte (Cloudflare Pages, Vercel). **Não** faz parte do app: o `localhost:3000` abre direto no login, porque quem abre ele já instalou. `site/` e `bench/` têm `export-ignore` e não vão no tarball do release.
- **`core/spec.py`** — `Spec` é o contrato central/agent: valida runtime, portas 9000-9099, gpu_memory_utilization, etc. Trafega como dict (`to_dict`/`from_dict`) em toda chamada de deploy.

### Ciclo de vida de um deploy

`checking` (preflight no agent — aborta se algum check falhar) → `provisioning` → `healthy` | `failed`; `redeploying` para re-subida. O central roda `_reconcile_loop` a cada 10s: deploy `healthy` cujo processo/container sumiu no agent vira `failed`. O reconcile de startup cobre órfãos em `provisioning`/`redeploying`.

### Runtimes

- **vLLM** — docker `vllm/vllm-openai:v0.21.0`, exige NVIDIA + Docker. Rejeita repo GGUF-only (precisa de safetensors).
- **llama-server (híbrido)** — com NVIDIA (`nvidia-smi`): docker `ghcr.io/ggml-org/llama.cpp:server-cuda`. Sem NVIDIA: binário nativo do release oficial **pinado** (`BIN_VERSION`, sha256 conferido via GitHub API) baixado em `llama-bin/`. Detecta `libcuda` para build CUDA nativa sem Docker. A imagem é a **`:server-cuda`** (a `:server` é só-CPU: com ela o card ficava em 0 MiB) e o comando leva **`-ngl auto`**, nunca um número fixo — `auto` deixa o `--fit` do llama.cpp dividir entre GPU e CPU um modelo maior que a VRAM, e um número fixo desliga isso. `--cache-ram` é limitado a 1/8 da RAM: o padrão de 8 GB é maior que o WSL e o servidor morria por OOM sob tráfego contínuo.
- GGUF vai para `llama-models/<org>--<model>/`, magic bytes `GGUF` validados. VLM detectado por `mmproj-*.gguf`.
- `llama-bin/`, `llama-models/`, `sursumai.db*` e `sursumai-logs/` são ignorados pelo git.

### Endereçamento OpenAI

Uma URL só (`http://localhost:8001/v1`) e o `model` decide quem responde. `_resolve_target()` resolve nesta ordem, para nunca ficar ambíguo: `router`/`auto` → pool padrão; id exato de deploy; id ou nome de pool; nome do modelo (`Qwen/Qwen3-0.6B-GGUF`). Modelo desconhecido devolve 404 listando o que existe; modelo não pronto devolve 422 com o status. `/v1/models` lista deployments, pools e `router`.

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

Quatro segredos, quatro donos:

- **API key da conta** (`api_keys`, prefixo `sk-sursum-`) é o que o usuário usa: uma ou várias, nomeadas, revogáveis, guardadas com sha256 e mostradas em texto puro uma única vez. Vale para todos os modelos e pools da conta — chave por deployment estava errado, ninguém tem uma chave por modelo na OpenAI ou na Anthropic. **Escopo é só `/v1`**: `_api_user` aceita chave ou sessão; `_current_user` (gerenciamento) recusa chave com 403, para que uma chave vazada de um script não consiga destruir deployment nem criar outra chave.

- **`AGENT_KEY`** (`core/keys.py`) protege o agent. Gerada aleatória no 1º run em `~/.sursumai/agent.key` (0600); `AGENT_KEY` no ambiente ganha. Comparação sempre por `hmac.compare_digest`. O agent recusa subir com a chave de dev fora de loopback, e a checagem é um **middleware** — como dependência ela rodava depois da validação do corpo, devolvendo 422 antes de 401.
- **`Spec.api_key`** (prefixo `sk-internal-`) é **interno**, nunca exposto: tranca a porta do modelo para que nada mais na máquina fale com ela. Gerada em `POST /deploys`. Toda chamada ao endpoint do deploy (playground, router, judge, health probe, métricas) manda esse bearer; os specs ficam em `~/.sursumai/specs/` (0600) para sobreviver a um restart do agent. **Nunca colocar a chave em argv** — `" ".join(cmd)` vai para o log do deploy, que o README manda o usuário abrir quando algo falha, e argv é legível em `/proc/<PID>/cmdline`. llama-server lê de `--api-key-file` (arquivo 0600 em `~/.sursumai/deploys/`, montado read-only no container); vLLM não tem essa opção e recebe por `-e VLLM_API_KEY` (flag `-e` sem valor: o docker herda do processo chamador). O `-e`/`-v` tem que vir **antes** da imagem — depois dela tudo é argumento do servidor do modelo.
- **`auth_enforced`** no status do agent: depois de saudável, o agent faz uma sonda *sem* a chave e confirma que leva 401/403. Se um runtime ignorasse a chave, ele subiria aberto e a sonda autenticada passaria igual — controle de segurança que falha em silêncio é pior que nenhum. O central loga `error` se isso acontecer.
- **Token de sessão** protege a API do central, como acima.

### Bind

Os 3 processos **e as portas dos deploys (9000-9099)** escutam em `127.0.0.1`. Exposição na rede é opt-in por `SURSUMAI_BIND` (respeitado por `start.sh`, `web/server.py`, o CLI e `ports.bind_host()`, que os executores usam no `--host` do llama e no `-p` do docker). Nunca voltar `0.0.0.0` como default: a porta do modelo publicada em `0.0.0.0` deixava o modelo na internet de qualquer VM com IP público, com a chave do deploy como única barreira — e em alguns hosts o bind em `0.0.0.0` é recusado e o servidor nem sobe.

## Peculiaridades

- SQLite com `check_same_thread=False` + WAL — necessário porque o central usa threads para o `agent_client`.
- Migrações em `central/db.py` são `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`; adicionar coluna nova segue esse padrão (não recriar tabela). Exceção deliberada: `sessions` foi recriada porque token em texto puro não migra.
- Ciclo de vida das apps é `lifespan` (não `@app.on_event`, depreciado na versão pinada do FastAPI); o `lifespan` do central cancela os loops de background ao encerrar.
- **Não engolir exceção.** `except Exception: pass` some com bug real (o `_metrics_loop` fazia isso e falha de scrape ficava idêntica a modelo ocioso). Use `log.debug` para o esperado e `log.exception` para o resto.
- **API 100% default**: não mexer no thinking do modelo — sem `enable_thinking:false`. Reasoning conta em `completion_tokens` (verificado empiricamente). Um modelo pode gastar todo o contexto em reasoning (`content` vazio, `finish:length`) — aceito.
- **Timeout de chat é `SURSUMAI_CHAT_TIMEOUT` (900 s)**, não 180: modelo que raciocina num notebook pensa por minutos, e o timeout antigo cortava e dizia "unreachable" com o modelo vivo. Timeout diz que o modelo está demorando; só conexão recusada diz unreachable. O juiz do router tem `JUDGE_MAX_TOKENS = 1024` pelo mesmo motivo — com 120 um juiz que pensa devolvia veredito vazio e o router nunca escalava. Preflight de porta espera até `SURSUMAI_PORT_WAIT` (90 s) por uma porta recém-liberada (o Docker Desktop leva 30 s+).
- **URL que o usuário copia é sempre absoluta e sempre `/v1`.** `API` (`"/api"`) é relativo e só funciona dentro da página, pelo proxy do web; snippets, Base URL e o curl do Playground usam `publicBaseUrl()` (`http://<host>:8001/v1`). Um snippet com `base_url="/api/v1"` quebrou no primeiro `python`, e o curl de modelo apontava para `/deploys/{id}/chat` — rota de gerenciamento que recusa API key com 403.
- Resposta direta de um deploy volta com `model` = `spec.model`: o llama-server devolve o caminho do GGUF no container (`/models/…gguf`). Vale para resposta normal e streaming (`_relabel_upstream` com `session_id=None`).
- **UI sem enchimento.** Card saudável não mostra o checklist do preflight (fica no Details); métrica que o runtime não exporta (llama: requests, TTFT, KV, latência, token/s) não vira quadrado com "—", some; o reasoning do modelo fica recolhido num `<details>`; markdown básico é renderizado com o HTML escapado antes (`renderMarkdown`). O card do pool mostra o estado real (verde só com todos os membros rodando, vermelho com menos de 2) e o modal de pool pré-seleciona do menor para o maior modelo (`paramsOf` pelo nome) — a ordem é a configuração do router.
- O proxy de chat (`POST /deploys/{id}/chat`) aceita `messages: list[dict]` genérico, para o playground mandar `image_url` (data URL base64) em modelos de visão. O botão "Image" só habilita se o preflight trouxe `vision.ok`.
- `requirements.txt` é mínimo e **todo pinado com `==`** (fastapi, uvicorn, huggingface_hub, python-dotenv): instalador que resolve "latest" dá um build diferente para cada usuário. vLLM vem da imagem docker, nunca do pip.
- `VERSION` é lido por `/meta/update` e por `sursumai update`. O "latest" vem do **release mais novo** na API do GitHub, não de `raw/main/VERSION`; o update baixa o `install.sh` da própria tag e roda com `SURSUMAI_VERSION=v<X.Y.Z>`. Ao lançar: bump em `VERSION`, no `SURSUMAI_PINNED` do `install.sh`, no comando de instalação do README e do site (`site/index.html`, `site/site.js`), e publique o `SHA256SUMS` do tarball no release (o instalador aborta se o hash não bater, e só avisa se não houver hash publicado).

### Uma edição só

Free e Pro são **o mesmo build**: mesma pasta (`~/sursumai`), mesmo comando, mesmo banco.

**Em transição** (ver *Estado atual*): o que está lançado decide a edição pelo **token do GitHub**, e o que está sendo construído decide por **chave de licença** (`core/license.py`). O alvo é a chave; o token sai quando ela estiver completa.

#### Pelo token (v0.9.0, no ar)

O que muda é **de onde vem a atualização**, e quem decide isso é o token:

- `curl … | bash` → repo público (free). `curl … | GITHUB_TOKEN=… bash` → repo privado (Pro).
- O instalador grava `~/.sursumai/edition.json` (0600, guarda o token). `core/edition.py` é a autoridade: `repo()`, `token()`, `is_pro()`, `api_headers()`. Ambiente (`SURSUMAI_TOKEN`) ganha do arquivo.
- Sem esse arquivo, o Pro clicava em "Update" e **voltava para o free**. O `/meta/update` e o `sursumai update` leem a edição antes de procurar release, e para o Pro buscam o `install.sh` pela **API do repo privado** (a tag do Pro não existe no público).
- Release privado não tem `SHA256SUMS` público: o tarball vem da API autenticada (`/tarball/<tag>`), TLS + token no lugar do hash. Token **nunca** em argv — vai por env (`SURSUMAI_TOKEN`), como toda chave neste projeto.

#### Pela chave de licença (alvo)

- `core/license.py` verifica `SURSUM-<payload base64url>.<assinatura base64url>` com Ed25519 (`core/ed25519.py`, Python puro — `requirements.txt` tem quatro pacotes de propósito, e checagem de licença não pode ser o motivo de uma instalação falhar).
- **Offline, sempre.** Licença que telefona para casa transforma queda nossa em "os recursos que você pagou sumiram", num produto cujo ponto é rodar na máquina do cliente. Arquivo corrompido = edição grátis, nunca app que não sobe.
- Mensagens de erro são escritas **para o comprador** (chave incompleta / danificada / de outra pessoa), e `base64` decodifica com `validate=True`: sem isso o Python descarta caracteres inválidos em silêncio e uma chave digitada errado voltava como "chave inválida", mandando o cliente pedir reembolso em vez de copiar de novo.
- Isso **não impede** ninguém de editar o código e remover a checagem — nada que roda na máquina do cliente impede. É a linha entre usar o Pro e não pagar por ele, como Sublime Text e GitLab fazem.
