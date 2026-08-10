#!/usr/bin/env bash
# SursumAI — start all 3 processes (Web 3000 / Central 8001 / Agent 8010)
# Persistent: does NOT delete sursumai.db. State survives restarts.
# Non-engineer friendly: first run bootstraps setup.sh automatically.

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
LOGDIR="/tmp/opencode"
mkdir -p "$LOGDIR"

export AGENT_URL="${AGENT_URL:-http://localhost:8010}"
export AGENT_KEY="${AGENT_KEY:-dev-agent-key}"

# First run: bootstrap venv + deps (setup.sh is idempotent and friendly).
if [ ! -x "$PY" ]; then
  echo "First run detected — setting up the environment…"
  bash "$ROOT/setup.sh"
fi

pkill -f "uvicorn agent.app" 2>/dev/null || true
pkill -f "uvicorn central.app" 2>/dev/null || true
pkill -f "web/server.py" 2>/dev/null || true
sleep 1

echo "Starting Agent (8010)..."
setsid nohup "$PY" -m uvicorn agent.app:app --host 0.0.0.0 --port 8010 \
  >> "$LOGDIR/agent.log" 2>&1 &

echo "Starting Central (8001)..."
setsid nohup "$PY" -m uvicorn central.app:app --host 0.0.0.0 --port 8001 \
  >> "$LOGDIR/central.log" 2>&1 &

echo "Starting Web (3000)..."
setsid nohup "$PY" web/server.py --port 3000 --host 0.0.0.0 \
  >> "$LOGDIR/web.log" 2>&1 &

sleep 2
echo "---"
echo "Web:     http://localhost:3000"
echo "Central: http://localhost:8001"
echo "Agent:   http://localhost:8010"
echo "Logs:    $LOGDIR/{agent,central,web}.log"

# Open the browser for the user (WSL on Windows, xdg-open elsewhere).
URL="http://localhost:3000"
if grep -qi "microsoft" /proc/version 2>/dev/null; then
  cmd.exe /c start "$URL" 2>/dev/null || true
elif command -v xdg-open >/dev/null 2>&1; then
  (xdg-open "$URL" 2>/dev/null || true) &
fi
