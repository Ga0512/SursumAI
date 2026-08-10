# CLI + Desktop — SursumAI como o Ollama

> "Instala, clica, pronto." Non-engineer usa um **ícone no desktop**; dev usa `sursumai`
> no terminal; headless/CI usa o mesmo comando sem UI. Um único `install.sh` cuida de tudo.

## Visão de produto

| Quem | Como usa |
|---|---|
| Non-engineer | Ícone no desktop → sobe tudo → browser |
| Dev com terminal | `sursumai` → sobe tudo → browser, ou comandos diretos |
| Headless / CI | `sursumai deploy unsloth/Qwen3.5-0.8B-GGUF` (sem UI) |

Regra: o CLI é um **daemon manager** por cima dos 3 processos existentes
(Web 3000 / Central 8001 / Agent 8010) + um **wrapper fino da API do central**.
Nada de novo no backend — só uma casca.

---

## T-1 CLI `sursumai` (launcher)

`sursumai/bin/sursumai` — script único (bash ou python), auto-contido.

- [ ] `sursumai` sem argumentos → sobe os 3 processos (reusa a lógica do `start.sh`)
      e abre o browser em `http://localhost:3000`.
- [ ] `sursumai --ui` → idêntico (nome explícito pro atalho do desktop).
- [ ] Detecta se os processos **já estão rodando** (health check em 8001/8010/3000)
      → se sim, só abre o browser (sem duplicar processos).
- [ ] `sursumai status` → mostra central/agent/web: running/stopped + URL.
- [ ] `sursumai stop` → derruba os 3 processos (sem matar deploys de modelo? **decidir**:
      provavelmente sim, deploy de modelo é independente e segue vivo).

**Critério de aceite:** `install.sh` → `sursumai` → browser abre com o app rodando.

## T-2 Comandos de controle (wrapper da API do central)

`sursumai deploy|list|logs|destroy` — chamam a API de 8001 (mesma auth do browser).

- [ ] `sursumai login <email> <senha>` (ou `sursumai login`) → guarda token em
      `~/.config/sursumai/auth.json` (permissão 0600).
- [ ] `sursumai list` → tabela: id, modelo, runtime, status, endpoint.
- [ ] `sursumai deploy <org/modelo> [--runtime vllm|llama] [--gpus 1]` → cria deploy
      (fluxo `checking → provisioning → healthy`), imprime a URL ao ficar healthy.
- [ ] `sursumai logs <id> [--tail 300]` → tail do log do deploy.
- [ ] `sursumai destroy <id>` → destroy com confirmação.
- [ ] Erros em linguagem humana (mesmo padrão da UI, sem stacktrace).

**Critério de aceite:** ciclo completo pelo terminal: `login → deploy → list → logs → destroy`.

## T-3 Segurança no CLI

- [ ] Token armazenado só no `~/.config/sursumai/` com `chmod 600`.
- [ ] `sursumai logout` remove o token.
- [ ] Sem segredo em log; sem `echo` de token.
- [ ] `sursumai` com token expirado → erro claro: "login expired — run `sursumai login`".

## T-4 install.sh cria o comando no PATH

`install.sh` (já existe) ganha o passo de instalação do CLI.

- [ ] Instala `sursumai` em `~/.local/bin/sursumai` (Linux/Mac) como symlink ou cópia.
- [ ] Adiciona `~/.local/bin` ao PATH se faltar:
      - Linux: `.bashrc` / `.zshrc`
      - macOS: `.zprofile` (PATH do `~/.local/bin` **não vem por padrão no Mac**).
- [ ] Idempotente: rodar de novo atualiza o `sursumai` sem duplicar entradas de PATH.
- [ ] `command -v sursumai` funciona depois do install.

**Critério de aceite:** `curl install.sh | bash` → `sursumai` disponível no terminal.

## T-5 Ícone no desktop (Linux)

- [ ] install.sh cria `~/.local/share/applications/sursumai.desktop`:
      `Exec=~/.local/bin/sursumai --ui`, com ícone e nome "SursumAI".
- [ ] Copia um asset de ícone (`.png`/`.svg`) para `~/.local/share/icons/`.
- [ ] `update-desktop-database ~/.local/share/applications` após criar.

**Critério de aceite:** o ícone aparece no menu/lançador e abre o app num clique.

## T-6 Ícone no desktop (Windows via WSL)

- [ ] install.sh detecta WSL (`/proc/version` com "microsoft") e cria um atalho
      no Desktop do Windows: `.bat`/`.lnk` que roda `wsl -e ~/.local/bin/sursumai --ui`.
- [ ] O atalho abre o browser via WSL (já funciona no `start.sh` atual).

**Critério de aceite:** duplo clique no Desktop do Windows → WSL sobe tudo → browser.

## T-7 Ícone no desktop (macOS)

- [ ] install.sh detecta Darwin e cria um `.app` mínimo (ou Automator/AppleScript)
      que roda `~/.local/bin/sursumai --ui`.

**Critério de aceite:** duplo clique no Launchpad/Finder → browser abre.

## T-8 Asset de ícone/logo

- [ ] Criar/definir o ícone oficial do produto (compatível com .png, .svg, .desktop, .lnk, .app).
- [ ] Nome do produto final decidido antes do ícone (o "SursumAI" do código é provisório —
      ver brainstorm de naming). **Blocker:** T-4→T-7 dependem do nome.

---

## Ordem de execução

1. **T-8** (nome + ícone — destrava tudo)
2. **T-1** (launcher) → **T-4** (PATH no install.sh)
3. **T-2 + T-3** (comandos + segurança)
4. **T-5 → T-6 → T-7** (ícones por SO)
5. Validação non-engineer de ponta a ponta: `install → sursumai → browser → deploy → ícone`.

## Fora de escopo (agora)

- Auto-start com o SO (systemd/LaunchAgent/Windows service) — o `sursumai` sobe
  manualmente; auto-start fica pra depois (o deploy 24/7 enquanto o PC liga já vale).
- Binário compilado único (empacotar em Rust/Go) — o CLI começa como script.
