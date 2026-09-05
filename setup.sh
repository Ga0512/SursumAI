#!/usr/bin/env bash
# SursumAI — first-time setup (venv + dependencies + friendly checks)
# Non-engineer friendly: run once, it prepares everything start.sh needs.

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYBIN="$(command -v python3 || command -v python || true)"

echo "◆ SursumAI — setup"
echo

if [ -z "$PYBIN" ]; then
  echo "✗ No Python found. Install Python 3.10+ first:"
  echo "    https://www.python.org/downloads/"
  echo "  (on Windows, enable WSL and run: sudo apt install python3 python3-venv)"
  exit 1
fi

# `docker info` blocks for a long time when Docker is installed but the daemon
# is not running - very common on WSL with Docker Desktop closed. This script is
# often the first thing a new user runs, and a silent hang is the worst possible
# first impression, so every probe is bounded. macOS ships no timeout(1), hence
# the fallback.
run_with_timeout() {
  local secs="$1"; shift
  local rc=0
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@" || rc=$?
    return "$rc"
  fi
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local watchdog=$!
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watchdog" 2>/dev/null || true
  return "$rc"
}

docker_running() {
  command -v docker >/dev/null 2>&1 || return 1
  run_with_timeout 10 docker info >/dev/null 2>&1
}

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  "$PYBIN" -m venv .venv
fi

echo "Installing dependencies… (a few minutes on the first run)"
.venv/bin/python -m pip install --quiet --upgrade pip
# deliberately NOT --quiet: on WSL this takes minutes, and silence looks
# exactly like a hang - which is the moment people give up
.venv/bin/python -m pip install -r requirements.txt

echo
echo "Checking Docker (used for NVIDIA GPU inference)…"
if command -v docker >/dev/null 2>&1; then
  if docker_running; then
    echo "✓ Docker is running."
  else
    echo "! Docker is installed but NOT responding."
    echo "  Start it (Docker Desktop) and try again, or keep going —"
    echo "  without Docker we can still run models via the native llama.cpp binary."
  fi
else
  echo "! Docker is not installed."
  echo "  Optional: install Docker Desktop from https://www.docker.com/products/docker-desktop/"
  echo "  It unlocks NVIDIA GPU inference (vLLM / llama.cpp with CUDA)."
  echo "  Without it, SursumAI still works using the native llama.cpp binary (CPU)."
fi

echo
echo "✓ Setup complete. Run ./start.sh to launch."
