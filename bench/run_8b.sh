#!/usr/bin/env bash
# The 8B column only: the 0.6B answers and the judges' verdicts do not depend
# on who the strong model is, so they are reused from runs/gsm8k-100.
set -u
cd "$(dirname "$0")/.."
RUN=bench/runs/gsm8k-100-8b
PY=.venv/bin/python
$PY bench/cleanup.py --all
sleep 10
$PY -u bench/run_models.py --run "$RUN" --models Qwen/Qwen3-0.6B-GGUF Qwen/Qwen3-8B-GGUF || exit 1
$PY bench/score.py --run "$RUN" | tee "$RUN/table.txt"
echo BENCH_DONE
