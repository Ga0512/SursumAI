# MVP — Self-Hosting Models (SursumAI)

> "Your model. Your infra. Your URL."
> Self-hosted LLM inference in one click — on your account, at your cost.

Produto: plataforma de deploy de modelos open-source (self-hosting) tão fácil quanto
publicar um site na Vercel. O usuário escolhe um modelo, clica **Deploy**, e recebe uma
URL OpenAI-compatível rodando **na infra dele** (local ou cloud via BYOC).

Runtime: **vLLM nativo via `vllm serve`** — o servidor OpenAI oficial do vLLM, que é
**exatamente** o formato OpenAI (streaming, todos os parâmetros, usage real, tool
calling). SEM código custom (`api_server.py` NÃO é usado no MVP). O runtime é o
`vllm/vllm-openai` direto.
Control plane: **novo** (FastAPI + UI). Multi-provider via **Terraform** (MVP: AWS).

## Decisões-chave (fixadas)

- **Web-first**: a interação é sempre via web (local e nuvem). Não há CLI pro usuário;
  `curl` só instala. Local = a web app roda na máquina dele; nuvem = web app hosteada.
- **1 processo**: backend (FastAPI) serve a API **e** o frontend (HTML/JS estático) na
  mesma porta. Sem CORS, sem 2 builds. Migra pra separado depois, sem retrabalho.
- **Deploy é a unidade**: 1 deploy = 1 modelo + infra própria + endpoint próprio.
  Usuário tem N deploys independentes (local, cloud, qualquer combinação).
- **Runtime**: `vllm/vllm-openai` com `vllm serve` — OpenAI **exato** (streaming, params,
  usage, tool calling). Nenhum código custom. `api_server.py`/`handler.py` ficam de fora
  do MVP.
- **Re-deploy ilimitado**: o usuário pode re-deployar o MESMO deploy quantas vezes
  quiser, mudando qualquer parâmetro (ex: `gpu_memory_utilization`, GPUs, modelo).
  O deploy preserva `id` e `endpoint`; a infra é recriada com o novo spec.
- **Transportes do MVP**: `local` (docker) e `aws` (terraform). Multibox/k3s fica
  depois. GPUs numa máquina só = docker com `--tensor-parallel-size`, qualquer quantidade.
- **BYOC**: usuário traz a conta cloud. No MVP: API key. Testado barato (RunPod/Vast.ai)
  antes de AWS de verdade.
- **UI/UX**: inglês, tema claro, **Liquid Glass** (não glassmorphism), tokens como
  identidade visual, fontes arredondadas. Foco em "Self-Hosting".
- **Hero dinâmico**: "Self-Hosting models for …" rotativo: `open weights` /
  `fine-tuned models` / `your own cloud (BYOC)` / `unlimited tokens` /
  `— your infra, your models, your call`.

## Arquitetura

```
web/ (HTML/JS estático — Liquid Glass UI)
core/ (FastAPI — API + serve a web)
 ├── spec.py           lê/valida o spec
 ├── state.py          SQLite (deploys)
 ├── transports/
 │    ├── local.py     docker run + TP
 │    └── terraform.py terraform apply AWS
 └── api.py            POST/GET/DELETE /deploys (+ redeploy)
deploy/                runtime vLLM (existente, intocado)
spec.yaml              contrato de entrada (exemplo)
```

## Contratos

### spec.yaml
```yaml
model: Qwen/Qwen3-0.6B
target: local            # local | aws
gpus: 1                  # → --tensor-parallel-size
nodes: 1                 # 1 = docker; >1 = (futuro) cluster
gpu_memory_utilization: 0.50
max_model_len: 300
max_tokens: 2048
temperature: 0.0
```

### Deploy (estado)
```
id, spec, status (pending → provisioning → healthy/failed), endpoint, created_at, updated_at
```

### Endpoint (contrato de saída)
```
{ base_url, api_style: openai, status }
```

> **OpenAI exato**: o endpoint é o servidor OpenAI nativo do vLLM (`vllm serve --model`).
> Streaming, `top_p`, `stop`, `usage` real, tool calling — tudo igual à API da OpenAI.
> O `api_server.py` custom (enable_thinking/response_format) fica de fora do MVP;
> se for necessário, entra depois como opção.

---

# Backlog — Épicos e Tickets

## Épico 1 — Fundação (contratos estáveis)

### T-1 Spec schema + validação
- Definir `spec.yaml` completo (campos acima).
- Validação: tipos, ranges (mem util 0–1, max len > 0), `gpus≥1`, `nodes≥1`.
- `gpus > 1` ⇒ habilita `tensor-parallel-size`.
**Done:** spec de exemplo válido; validação rejeita specs inválidos com mensagem clara.

### T-2 Estado do deploy (SQLite)
- Modelo `Deploy`: `id` (uuid), `spec`, `status`, `endpoint`, timestamps.
- Persistência simples (SQLite). Sem ORM pesado.
**Done:** deploys sobrevivem a restart; consulta por id/listagem funciona.

### T-3 Contrato de endpoint
- Formato único `{ base_url, api_style: openai, status }` pra local e nuvem.
**Done:** local e aws devolvem o MESMO formato.

## Épico 2 — Core (backend, 1 processo)

### T-4 API de deploys (FastAPI)
- `POST /deploys` (cria + dispara deploy), `GET /deploys`, `GET /deploys/{id}`,
  `DELETE /deploys/{id}`.
- `POST /deploys/{id}/redeploy` com novo spec (re-deploy).
**Done:** CRUD completo + redeploy, com estado persistido e consistente.

### T-5 Transporte `local`
- Monta e roda `docker run` da imagem `vllm/vllm-openai` com o **servidor OpenAI nativo**:
  `vllm serve --model {MODEL} --tensor-parallel-size {gpus} --gpu-memory-utilization …
  --max-model-len …`. Mesma porta (8000) a cada re-deploy.
- Container nomeado por `deploy-{id}` (re-deploy = stop + run no mesmo nome).
- `api_server.py` custom NÃO é usado.
**Done:** um deploy local fica `healthy` e devolve `http://localhost:8000/v1` (OpenAI exato).

### T-6 Health check
- Poll `/v1/models` até responder (timeout configurável), atualiza status.
- Trata: container morreu, porta não abriu, modelo falhou ao carregar.
**Done:** status vira `healthy`/`failed` de verdade (nunca trava em `pending`).

### T-7 Erros graciosos
- GPU ausente, VRAM insuficiente, Docker desligado, modelo inválido/inexistente,
  falha de download → mensagem clara em inglês na API **e** na UI.
- Nenhum stacktrace vaza pro usuário.
**Done:** cada falha conhecida tem mensagem legível; a UI mostra o problema.

### T-8 Destruir deploy
- `DELETE /deploys/{id}` → `docker stop` (local) / `terraform destroy` (aws) →
  remove do estado.
**Done:** recursos reais são removidos; deploy some da listagem.

### T-9 Re-deploy (idempotente)
- `redeploy` com novo spec: preserva `id` e `endpoint`; mata a infra antiga e
  recria com o novo spec.
- Re-deploy com spec idêntico não quebra (no-op ou recria sem erro).
- Estados visíveis: `redeploying`.
**Done:** mudar `gpu_memory_utilization`/GPUs e re-deployar funciona sem perder a URL.

## Épico 3 — Web UI (Liquid Glass, inglês)

### T-10 Design system "Liquid Glass"
- **Liquid Glass** (não glassmorphism): superfícies com profundidade física,
  contornos internos brilhantes (inner border), reflexos/highlights, blur de fundo
  forte, elementos que parecem flutuar em líquido. Cantos bem arredondados.
- Tema claro; fontes arredondadas; cores dos 5 pilares como acentos.
- Tokens (chips coloridos) como identidade visual.
**Done:** components base: glass card, chip, botão, input, badge de status, toast.

### T-11 Landing + Hero dinâmico
- "Self-Hosting models for …" com rotação de palavras-chave (5 pilares), cada uma
  com cor própria, animação tipo "tokens sendo completados" + cursor piscando.
- Subtexto estático: "Self-hosted LLM inference, one click away — on your account,
  at your cost."
- Vitral de tokens desfocado no fundo.
**Done:** rotação suave, sem quebra de layout, 100% inglês.

### T-12 Login
- Painel liquid glass central + "Continue with Google/GitHub".
- (Auth pode ser mock no MVP; a conta e seus deploys persistem.)
**Done:** login (mock) → cai no dashboard; sessão mantém os deploys.

### T-13 Dashboard
- Lista de deploys como **glass cards**: modelo, target, GPUs, status (bolinha),
  URL + botão copy (feedback), ações (re-deploy, destroy).
- Empty state lindo quando não há deploys.
- Botão [+ New].
**Done:** cada card reflete estado real da API; copiar URL funciona; destroy/re-deploy
disparados da UI.

### T-14 Novo Deploy (modal/sheet)
- Modelo (campo livre + chips de sugestão), Target (Local/AWS), GPUs, Nodes, e os
  campos de runtime (mem util, max len, max tokens, temperature).
- Botão Deploy (único acento vivo). Progresso visual com tokens "contados" até healthy.
**Done:** criar deploy da UI → aparece no dashboard com a URL.

### T-15 Backend serve a UI
- FastAPI serve HTML/JS estático na mesma origem (sem CORS).
**Done:** `GET /` abre a landing; dashboard consome a API na mesma porta.

## Épico 4 — Nuvem (o produto)

### T-16 Transporte `aws` (terraform)
- Gera módulo terraform: VPC + security group + EC2 GPU + user-data que sobe a imagem
  `vllm/vllm-openai` com `vllm serve --model {MODEL}` + flags do spec. Roda
  `terraform apply` (com state por deploy).
- Testar PRIMEIRO com provider barato (RunPod/Vast.ai) antes de AWS.
**Done:** deploy aws vira `healthy` com URL pública (OpenAI exato); destroy remove tudo.

### T-17 Credenciais cloud
- Usuário cola API key/credenciais na UI; guardadas criptografadas; nunca em log.
- Validação no momento de criar deploy cloud.
**Done:** deploy aws usa creds do usuário, sem vazar segredos.

## Épico 5 — Instalador (local)

### T-18 Install script (`curl | bash`)
- Detecta SO, GPU (`nvidia-smi`), driver, Docker; instala o que falta ou orienta
  claramente; sobe a web app; abre o browser em `http://localhost:3000`.
- Sempre em inglês, mensagens por etapa, falha graciosa.
**Done:** máquina com GPU+Docker → `curl` → app no browser em <2 min.

---

## Fora de escopo (MVP)
Multibox/k3s, agente on-prem, Azure/GCP, billing, multi-tenant, catálogo de modelos,
auth real, streaming de logs, dashboard de métricas.

## Ordem de execução
T-1 → T-8 (core, $0) → T-10 → T-15 (UI) → T-9 (redeploy) → T-16 → T-17 (nuvem) → T-18 (install).

## Riscos
- VRAM insuficiente (T-7 resolve com validação clara).
- Quotas de GPU em cloud barata (T-16 trata "instância indisponível").
- Runtime cloud ≠ runtime local ⇒ validar a MESMA imagem nos dois (T-5/T-16).
