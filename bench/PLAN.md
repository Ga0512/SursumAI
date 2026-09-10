# Plano do benchmark do router

**Escrito antes de rodar qualquer coisa** — mas commitado junto com o
resultado, não antes, então o git não prova a ordem. Fica registrado aqui para
não parecer o que não é. O ponto continua: as métricas e as colunas de controle
(moeda, oráculo) foram decididas antes de haver número pra olhar, e os desvios
do plano estão listados em [`RESULTS.md`](RESULTS.md).

## A pergunta

> Um LLM pequeno é suficiente pra decidir pra qual LLM mandar a tarefa?

## O que é medido

Duas colunas por política de roteamento, nos mesmos 300 itens:

| coluna | o que é |
|---|---|
| **correct** | quantos itens a política respondeu certo |
| **wakes strong** | em quantos ela precisou acionar o modelo grande |

E duas colunas de leitura, que existem pra impedir conclusão errada:

| coluna | o que é |
|---|---|
| **headroom** | onde a política cai entre "só o pequeno" (0%) e o oráculo (100%) |
| **vs coin** | acerto menos o de uma moeda jogada **na mesma taxa de escalação** |

`vs coin` é a coluna que decide. Acerto sobe sozinho conforme se escala mais,
independente de a decisão ser boa — comparar políticas com taxas diferentes é
comparar orçamentos diferentes. Uma política com `vs coin` ≤ 0 não está
decidindo, está só gastando.

Separado, a qualidade do juiz — recall, precisão, e os dois erros contados
apart, porque eles custam coisas diferentes:

- **missed**: o fraco errou e o juiz aprovou → o usuário recebe resposta ruim
- **wasted**: o fraco acertou e o juiz escalou → desperdício de compute

## Dataset

GSM8K, os **300 primeiros itens do split de teste oficial**, sem seleção.
Fonte: `openai/grade-school-math`, `test.jsonl`.

Verificação é numérica: extrai o número final da resposta e compara com o gold.
**Nenhum LLM avalia nada.** Usar um modelo pra julgar se um modelo respondeu bem
é exatamente a circularidade que esse benchmark existe pra evitar — e é por isso
que MBPP, MT-Bench e RAG do RouterBench ficaram de fora: o rótulo deles é do
GPT-4. MMLU, ARC, Hellaswag e Winogrande ficaram de fora por outro motivo:
resposta de múltipla escolha é uma letra, e julgar "essa letra está boa" não se
parece com o que o juiz faz na prática.

## Modelos

Ladder (do mais barato ao mais forte), todos GGUF em `llama-server`:

```
Qwen3-0.6B → Qwen3-1.7B → Qwen3-4B → Qwen3-8B
```

Juízes: 0.6B, 1.7B, 4B. `temperature=0` em tudo.

Um modelo residente por vez — 6GB não comporta dois, e isso também mantém as
medições de tempo honestas.

## Políticas comparadas

`only weak`, `stage`, `escalation` (por juiz), `classifier` (por juiz),
`round_robin`, `oracle`, `only strong`, e a moeda pareada por taxa.

A regra do `stage` e os prompts do juiz são **importados** de
`central/router.py`, nunca copiados. Benchmark de reimplementação mede a
reimplementação.

## O que este benchmark NÃO mede

Declarado antes, pra não virar desculpa depois:

- **`advisor` e o latch.** Ambos dependem de turno seguinte, e GSM8K é turno
  único. No `advisor` o juiz roda em background e o veredito só vale no próximo
  turno; sem próximo turno ele é idêntico a "só o pequeno". Precisa de um
  segundo experimento com conversas de vários turnos, e é lá que se decide se
  `CONFIRMATIONS = 2` é o número certo (hoje é chute).
- **Custo real de tempo.** O replay mede qualidade da decisão, não latência nem
  troca de modelo na VRAM. Na máquina alvo escalar pode significar descarregar
  um modelo e carregar outro, e isso provavelmente domina tudo. Fica pra uma
  fase 4, ao vivo, com as políticas vencedoras.
- **Generalização.** É GSM8K, ou seja, matemática. Não diz nada sobre código,
  conversa ou visão.

## Como será reportado

Os `.jsonl` brutos vão versionados junto com o `RESULTS.md`, então qualquer um
refaz o `score.py` e confere sem rodar modelo nenhum.

**Resultado negativo é publicado.** Se o juiz de 0.6B empatar com a moeda, ou se
o `stage` (grátis) ganhar dos modos com LLM, isso vai na tabela do mesmo jeito.
Uma tabela sem linha ruim é propaganda, não medição.

## Passos

```bash
python bench/dataset.py --items 300      # baixa GSM8K
python bench/run_models.py               # horas de GPU, retomável
python bench/run_judges.py               # menos de uma hora, retomável
python bench/score.py --run bench/runs/gsm8k
```
