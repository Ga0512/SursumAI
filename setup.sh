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

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  "$PYBIN" -m venv .venv
fi

echo "Installing dependencies…"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo
echo "Checking Docker (used for NVIDIA GPU inference)…"
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    echo "✓ Docker is running."
  else
    echo "! Docker is installed but NOT running."
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
