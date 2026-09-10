# Resultado do benchmark do router — GSM8K, 100 itens

Rodado em 2026-09-10, numa máquina com GPU NVIDIA de 6 GB, via WSL, com
`llama-server` na imagem CUDA. Plano e métricas fixados antes em
[`PLAN.md`](PLAN.md). Dados brutos em `runs/gsm8k-100/` — qualquer um refaz a
tabela com `python bench/score.py --run bench/runs/gsm8k-100`, sem GPU.

## Desvios do plano

O [`PLAN.md`](PLAN.md) previa 300 itens, ladder de quatro modelos (0.6B → 8B) e
três juízes. Rodamos **100 itens, três modelos (0.6B, 1.7B e 8B, sem o 4B) e
dois juízes (0.6B e 1.7B)**: com o modelo ainda na CPU (bug 1 abaixo) cada
pergunta levava ~70 s, e o plano completo passava de um dia de máquina. O 8B
entrou depois, só como o forte do segundo ladder. As métricas, o dataset e as
colunas de controle são os do plano.

## A pergunta

> Um LLM pequeno é suficiente pra decidir pra qual LLM mandar a tarefa?

## Os modelos sozinhos

| modelo | acerto | mediana por resposta |
|---|---|---|
| Qwen3-0.6B | 69% | 6,0 s |
| Qwen3-1.7B | 86% | 9,8 s |

## As políticas de roteamento

Ladder: Qwen3-0.6B (fraco) → Qwen3-1.7B (forte).

| política | acerto | chama o forte pra responder | vs moeda |
|---|---|---|---|
| só o 0.6B | 69% | 0% | — |
| `stage` (regra, sem juiz) | 69% | 1% | 0 |
| `escalation`, juiz 0.6B | 78% | 18% | +6 |
| **`escalation`, juiz 1.7B** | **85%** | **22%** | **+12** |
| `classifier`, juiz 0.6B | 77% | 76% | **−5** |
| `classifier`, juiz 1.7B | 80% | 52% | +2 |
| `round_robin` | 77% | 50% | −1 |
| oráculo | 88% | 19% | +16 |
| só o 1.7B | 86% | 100% | — |

**vs moeda** = acerto menos o de uma moeda que escalasse na mesma frequência.
Zero ou negativo significa que a política está gastando, não decidindo.

## A qualidade do juiz

O juiz lê a pergunta e a resposta do 0.6B — sem ver o gabarito — e diz se
escala.

| juiz | pegou os erros do 0.6B | precisão | erros que deixou passar | alarmes falsos |
|---|---|---|---|---|
| Qwen3-0.6B | 48% (15 de 31) | 83% | 16 | 3 |
| Qwen3-1.7B | 71% (22 de 31) | 100% | 9 | 0 |

## O que isso responde

**Juiz pequeno serve — quando ele julga, não quando ele adivinha.**

- No `escalation` o juiz lê uma resposta que já existe. Um juiz de 1.7B pegou
  71% dos erros sem nenhum alarme falso, e a política chegou a **85% de acerto
  contra 86% do modelo grande sozinho**, acionando o grande pra responder em
  22% das perguntas. É a linha com maior ganho sobre a moeda (+12).
- No `classifier` o juiz tem que escolher o modelo só pela pergunta, sem
  resposta pra olhar. Com juiz de 0.6B ficou **pior que a moeda** (−5); com
  1.7B, praticamente empatou (+2). Prever é muito mais difícil que verificar.
- O **`stage` não fez nada** no GSM8K: problema de matemática em linguagem
  natural quase nunca contém as palavras-chave da regra ("math", "equation",
  "prove"…). Disparou em 1 de 100. Regra de palavra-chave depende do tráfego.
- Juiz maior é melhor: de 0.6B para 1.7B o recall subiu de 48% para 71% e os
  alarmes falsos foram a zero.

## O que isso NÃO responde — e onde a tabela engana se lida rápido

**Nesse ladder, rotear não economiza tempo.** A coluna "chama o forte pra
responder" não inclui a chamada do juiz, e no `escalation` o juiz roda em toda
mensagem. Em mediana, por mensagem:

| política | conta aproximada | tempo |
|---|---|---|
| só o 1.7B | 9,8 | **9,8 s** |
| `escalation`, juiz 1.7B | 6,0 + 2,8 + 22% × 9,8 | **11,0 s** |
| `escalation`, juiz 0.6B | 6,0 + 1,5 + 18% × 9,8 | 9,3 s |

O 1.7B é só ~60% mais lento que o 0.6B. Com um gap tão pequeno, pagar a
resposta do fraco mais o juiz custa tanto quanto chamar o forte direto. O
roteamento **decide bem** (+12 sobre a moeda), mas só **paga** quando o modelo
forte é muito mais caro que o fraco — que é o caso do 8B, logo abaixo.

### Com um modelo forte caro de verdade: 0.6B → 8B

Mesmos 100 itens, mesmas respostas do 0.6B e mesmos vereditos dos juízes (eles
não dependem de quem é o forte) — só a coluna do forte foi gerada de novo, com
o Qwen3-8B. Nos 6 GB da máquina ele não cabe inteiro: o `-ngl auto` pôs 4,2 GB
na GPU e o resto na CPU, a 16 tokens/s. Dados em `runs/gsm8k-100-8b/`.

| política | acerto | chama o 8B pra responder | vs moeda | tempo médio por mensagem |
|---|---|---|---|---|
| só o 0.6B | 69% | 0% | — | 11,8 s |
| `escalation`, juiz 0.6B | 80% | 18% | +7 | **33 s** (11,8 + 1,9 + 18% × 107,1) |
| **`escalation`, juiz 1.7B** | **87%** | **22%** | **+13** | **39 s** (11,8 + 3,5 + 22% × 107,1) |
| oráculo | 93% | 24% | +18 | — |
| só o 8B | 93% | 100% | — | **107 s** |

Aqui o roteamento paga: com o juiz de 1.7B, **87% de acerto em ~39 s por
mensagem, contra 93% em ~107 s** chamando o 8B sempre — perto de 3x mais
rápido, perdendo 6 pontos. Com o juiz padrão (o membro mais barato do pool, o
próprio 0.6B), 80% em ~33 s.

Isso assume **os modelos já carregados**. Nos 6 GB desta máquina o 8B sozinho
ocupa 4,2 GB: o 0.6B e o 8B juntos não cabem, e o router teria que trocar de
modelo na VRAM a cada escalação. Essa troca não foi medida e provavelmente
engole o ganho. A conta acima vale para uma placa onde o pool inteiro fica
residente. O `classifier` não entra aqui: a escolha dele depende do ladder e
foi gerada para 0.6B → 1.7B.

Outras ressalvas:

- **100 itens.** O intervalo de confiança de cada acerto é de uns ±7 pontos.
  Diferenças de 1–2 pontos entre linhas não significam nada.
- **Só matemática.** Não diz nada sobre código, conversa ou visão.
- **Ladders de dois modelos.** No 0.6B → 1.7B o juiz de 1.7B é o próprio forte.
- **O juiz teve 1024 tokens.** A produção chamava o juiz com `max_tokens=120`,
  pouco pra um modelo que raciocina antes de responder — o veredito saía vazio
  e o router lia isso como "não escala". Corrigido junto com este benchmark:
  `central/router.py` agora usa `JUDGE_MAX_TOKENS = 1024`, o mesmo valor medido.
- `advisor` e o latch não foram medidos (precisam de conversa com vários turnos).

## O que o benchmark encontrou no produto

Rodar o mesmo modelo por uma hora seguida revelou bugs que nenhum teste pegava:

1. **O caminho NVIDIA + Docker nunca usou a GPU.** A imagem era
   `llama.cpp:server`, que é só-CPU. Com `server-cuda` e `-ngl auto`:
   **44 → 192 tokens/s** no 0.6B.
2. **O `llama-server` morria por falta de memória.** O cache de prompts em RAM
   vem com 8 GB por padrão, mais do que o WSL inteiro tem. Agora é limitado a
   1/8 da RAM.
3. **Timeout de 180 s com a mensagem errada.** O central desistia de esperar e
   dizia "deploy unreachable" com o modelo vivo e pensando. Agora espera 900 s
   (`SURSUMAI_CHAT_TIMEOUT`) e diz que o modelo está demorando.
4. **Porta recém-liberada recusada.** O preflight esperava 8 s pela porta de um
   deploy derrubado; o Docker Desktop leva mais de 30. Agora espera até 90 s.
5. **O juiz do router tinha 120 tokens** e podia devolver veredito vazio (ver
   as ressalvas acima). Agora 1024.

E um efeito de medição que quase passou: com orçamento de 1024 tokens, metade
das respostas do 0.6B foi cortada no meio do raciocínio e ele parecia acertar
44%. Com espaço pra terminar, acerta 69%. Orçamento apertado mede o
orçamento, não o modelo.
