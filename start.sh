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

# Bind to loopback by default -- SursumAI is a local app. Set SURSUMAI_BIND
# (e.g. SURSUMAI_BIND=0.0.0.0) to expose it to the network on purpose.
# The agent key is generated on first run into ~/.sursumai/agent.key; set
# AGENT_KEY yourself only if you want to choose the secret.
export SURSUMAI_BIND="${SURSUMAI_BIND:-127.0.0.1}"

if [ "$SURSUMAI_BIND" != "127.0.0.1" ] && [ "$SURSUMAI_BIND" != "localhost" ]; then
  echo "! Binding to $SURSUMAI_BIND -- SursumAI will be reachable from the network."
fi

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
setsid nohup "$PY" -m uvicorn agent.app:app --host "$SURSUMAI_BIND" --port 8010 \
  >> "$LOGDIR/agent.log" 2>&1 &

echo "Starting Central (8001)..."
setsid nohup "$PY" -m uvicorn central.app:app --host "$SURSUMAI_BIND" --port 8001 \
  >> "$LOGDIR/central.log" 2>&1 &

echo "Starting Web (3000)..."
setsid nohup "$PY" web/server.py --port 3000 --host "$SURSUMAI_BIND" \
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
