#!/usr/bin/env bash
# The whole benchmark, start to finish, unattended. Every step is resumable:
# if it stops, run this again and it continues where it was.
set -u
cd "$(dirname "$0")/.."
RUN=bench/runs/gsm8k-100
LADDER="Qwen/Qwen3-0.6B-GGUF Qwen/Qwen3-1.7B-GGUF"
PY=.venv/bin/python

# a model orphaned by a killed run holds VRAM the next one needs
$PY bench/cleanup.py --all
sleep 10
$PY -u bench/run_models.py --run "$RUN" --models $LADDER || exit 1
# a dead judge server stops the step; rerunning resumes where it stopped
for attempt in 1 2 3; do
  $PY bench/cleanup.py --all >/dev/null; sleep 10
  $PY -u bench/run_judges.py --run "$RUN" --judges $LADDER && break
  echo "judges step failed (attempt $attempt/3), retrying"
done
$PY bench/score.py --run "$RUN" | tee "$RUN/table.txt"
echo BENCH_DONE
