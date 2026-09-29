# AGENTS.md

**A orientação completa está em [`CLAUDE.md`](CLAUDE.md). Leia esse arquivo
antes de mexer em qualquer coisa** — arquitetura, runtimes, portas, router,
auth, segredos e as regras que saíram de testes reais e não podem regredir.

Este arquivo era uma segunda cópia da mesma coisa e envelheceu sozinho (chegou
a descrever uma chave de agent padrão que não existe mais, e um agent que só
rodava vLLM). Em vez de manter duas verdades, aqui fica só o que vale para
qualquer agente, e o resto tem um dono só.

## As três regras que não se negociam

1. **Nunca executar comandos silenciosamente.** Pergunta do usuário → responder
   com texto. Só rodar comando quando for pedido ("roda", "builda", "instala").
2. **O usuário-alvo é non-engineer.** A régua: instalar com um comando e usar.
   Decisão técnica é automática, não vira interruptor; erro é frase em
   linguagem humana, nunca stacktrace.
3. **README, documentação e mensagens de commit em português (pt-BR).** Código,
   API e interface em inglês.

## O mínimo para se localizar

```
web/server.py (3000) ──► central/app.py (8001) ──► agent/app.py (8010)
```

Central manda, agent executa, web serve a página. O Pro (máquinas por SSH) é um
módulo assinado, baixado pela conta que paga, e **não está neste repositório**.

```bash
bash start.sh      # sobe os três processos
python -m pytest   # a suíte inteira
```

Porta 8000 é de outro projeto — nunca usar.
