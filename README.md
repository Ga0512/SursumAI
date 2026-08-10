# SursumAI — Self-Hosting Models

> Seu modelo. Sua máquina. Sua URL.

O SursumAI sobe **modelos de IA open-source na sua própria máquina** e te dá uma URL
estilo OpenAI pra usar — tudo por uma interface web, sem precisar de terminal,
Docker, Python ou GPU pra começar.

Escolha um modelo, clique **Deploy**, e pronto: você ganha um link que qualquer
aplicativo OpenAI-compatible consegue usar.

---

## Requisitos

| Sistema | O que precisa |
|---|---|
| **Windows** | [WSL](https://learn.microsoft.com/pt-br/windows/wsl/install) instalado e aberto (Ubuntu recomendado) |
| **Linux** | Terminal — nada mais |
| **macOS** | Terminal — nada mais |

> **GPU NVIDIA?** Melhor ainda: o SursumAI detecta sozinho e usa o **vLLM** pra máxima
> performance (precisa do [Docker Desktop](https://www.docker.com/products/docker-desktop/)).
> Sem GPU NVIDIA, ele usa o `llama-server` — que roda em qualquer máquina, inclusive
> em imagens (VLM). Você não precisa decidir nada.

## Como rodar (primeira vez)

Dentro do terminal (WSL no Windows, Terminal no Linux/Mac):

```bash
git clone https://github.com/Ga0512/sursumai.git
cd sursumai
bash start.sh
```

Pronto. Na primeira vez o `start.sh` prepara tudo sozinho (Python, dependências e
checagens) e abre o browser em `http://localhost:3000`.

> **Em breve:** instalação de 1 comando estilo Ollama — `curl -fsSL <url> | bash` —
> sem precisar de `git` ou de clonar nada.

## Usar de novo

```bash
bash start.sh
```

O comando faz tudo: mantém seus deployments salvos e sobe os 3 serviços (Web, Central,
Agent). Se aparecer um aviso sobre Docker, leia a seção abaixo.

## Como funciona

1. Crie sua conta (email + senha).
2. Clique **+ New**, escolha um modelo entre os providers (Qwen, Kimi, DeepSeek,
   Llama, Mistral, Bonsai).
3. Clique **Deploy**. O SursumAI valida sua máquina, baixa o modelo e te dá a URL.
4. Use a URL em qualquer app OpenAI (Python, JavaScript, curl) — tem snippets prontos
   no botão **Details** de cada deployment.

### O que o SursumAI escolhe por você

- **Runtime** — vLLM se sua máquina tem GPU NVIDIA + Docker; `llama-server` caso
  contrário. O seletor fica visível no modal, mas você não precisa mexer.
- **Formato do modelo** — providers mostram modelos **safetensors** (para vLLM) e
  modelos **GGUF** (para `llama-server`), filtrados conforme o runtime escolhido.
- **Portas e chaves** — resolvidas e escondidas automaticamente.
- **Modelos vision** — se o modelo aceita imagens (Qwen-VL, Bonsai, etc.), o tab
  **Test** ganha um botão **Image** pra você enviar uma foto junto do texto.

### Providers de modelo

| Provider | Safetensors (vLLM) | GGUF (llama-server) |
|---|---|---|
| **Qwen** | Qwen3.6, Qwen3-VL… | Qwen3 30B MoE, 8B, VL-8B, 4B, 1.7B, 0.6B |
| **Kimi** | Kimi-K3, K2.6, K2.5, VL… | Kimi-K2 |
| **DeepSeek** | DeepSeek-V4… | R1 Distill 8B, 1.5B |
| **Llama** | Llama-4 Scout/Maverick | Llama-4 Scout, 3.3-70B |
| **Mistral** | Mistral Small/Medium/Large | Mistral Small 24B |
| **Bonsai** | — | Bonsai 8B, 4B, 1.7B, 27B (1-bit) |

## Docker (opcional, mas recomendado pra GPU)

O vLLM (GPU NVIDIA) roda dentro do Docker. Se o SursumAI avisar que o Docker não
está rodando:

1. Instale o **Docker Desktop**: https://www.docker.com/products/docker-desktop/
2. No Windows, abra as configurações do Docker e habilite a integração com WSL.
3. Rode `bash start.sh` de novo.

Sem Docker, o SursumAI continua funcionando com o `llama-server` nativo (CPU).

## Se algo der errado

- **"Could not reach server"** — os serviços não estão rodando. Verifique com
  `bash start.sh`.
- **Deploy ficou `failed`** — abra os **Logs** do deployment no card; a mensagem
  explica o problema em linguagem simples.
- **Modelo não aparece** — alguns modelos no Hugging Face exigem aceitar a licença.
  Use um dos modelos da lista que já estão liberados.

Logs completos dos serviços: `/tmp/opencode/{agent,central,web}.log`.

## Portas usadas

| Serviço | Porta |
|---|---|
| Web (interface) | 3000 |
| Backend Central | 8001 |
| Local Agent | 8010 |
| Deployments | 9000–9099 |
