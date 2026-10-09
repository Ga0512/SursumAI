#!/usr/bin/env bash
# SursumAI — start all 3 processes (Web / Central / Agent)
#
# The ports default to 3000 / 8001 / 8010 and are the host's decision when
# SursumAI runs as a service: SURSUMAI_WEB_PORT, SURSUMAI_CENTRAL_PORT and
# SURSUMAI_AGENT_PORT. core/ports.py validates them (the same three values the
# CLI and the dashboard use).
# Persistent: does NOT delete sursumai.db. State survives restarts.
# Non-engineer friendly: first run bootstraps setup.sh automatically.

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
LOGDIR="/tmp/opencode"
mkdir -p "$LOGDIR"

# `setsid` is GNU coreutils: macOS does not have it. nohup alone still detaches
# from the terminal there, which is all this needs.
if command -v setsid >/dev/null 2>&1; then DETACH="setsid"; else DETACH=""; fi

WEB_PORT="${SURSUMAI_WEB_PORT:-3000}"
CENTRAL_PORT="${SURSUMAI_CENTRAL_PORT:-8001}"
AGENT_PORT="${SURSUMAI_AGENT_PORT:-8010}"
export SURSUMAI_WEB_PORT SURSUMAI_CENTRAL_PORT SURSUMAI_AGENT_PORT
export AGENT_URL="${AGENT_URL:-http://localhost:$AGENT_PORT}"
export SURSUMAI_CENTRAL="${SURSUMAI_CENTRAL:-http://localhost:$CENTRAL_PORT}"

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

# A machine added over SSH runs the agent and nothing else: the dashboard, the
# database and the decisions stay on the user's own machine, and the agent
# there is reached only through the SSH tunnel.
AGENT_ONLY="${SURSUMAI_AGENT_ONLY:-0}"

pkill -f "uvicorn agent.app" 2>/dev/null || true
if [ "$AGENT_ONLY" != "1" ]; then
  # only the agent is ours to restart on a machine someone added over SSH —
  # it may run its own SursumAI dashboard, and that must keep running
  pkill -f "uvicorn central.app" 2>/dev/null || true
  pkill -f "web/server.py" 2>/dev/null || true
fi
sleep 1

echo "Starting Agent ($AGENT_PORT)..."
$DETACH nohup "$PY" -m uvicorn agent.app:app --host "$SURSUMAI_BIND" --port "$AGENT_PORT" \
  >> "$LOGDIR/agent.log" 2>&1 &

if [ "$AGENT_ONLY" = "1" ]; then
  # Whoever started this is a program on another machine, reading only the
  # exit code and the last line. Say whether the agent really came up, and if
  # not, the log line that explains why.
  for _ in $(seq 1 40); do
    if curl -sf http://127.0.0.1:$AGENT_PORT/health >/dev/null 2>&1; then
      echo "Agent only: http://127.0.0.1:$AGENT_PORT (reachable through the SSH tunnel)"
      echo "Logs:       $LOGDIR/agent.log"
      exit 0
    fi
    sleep 0.5
  done
  echo "The agent did not start. Last lines of $LOGDIR/agent.log:" >&2
  tail -n 5 "$LOGDIR/agent.log" >&2
  exit 1
fi

echo "Starting Central ($CENTRAL_PORT)..."
$DETACH nohup "$PY" -m uvicorn central.app:app --host "$SURSUMAI_BIND" --port "$CENTRAL_PORT" \
  >> "$LOGDIR/central.log" 2>&1 &

echo "Starting Web ($WEB_PORT)..."
$DETACH nohup "$PY" web/server.py --port "$WEB_PORT" --host "$SURSUMAI_BIND" \
  >> "$LOGDIR/web.log" 2>&1 &

sleep 2
echo "---"
echo "Web:     http://localhost:$WEB_PORT"
echo "Central: http://localhost:$CENTRAL_PORT"
echo "Agent:   http://localhost:$AGENT_PORT"
echo "Logs:    $LOGDIR/{agent,central,web}.log"
tr -d "[:space:]" < "$(dirname "$0")/VERSION" > "$LOGDIR/running-version" 2>/dev/null || true

# Open the browser for the user (WSL on Windows, xdg-open elsewhere).
URL="http://localhost:$WEB_PORT"
if grep -qi "microsoft" /proc/version 2>/dev/null; then
  cmd.exe /c start "$URL" 2>/dev/null || true
elif command -v xdg-open >/dev/null 2>&1; then
  (xdg-open "$URL" 2>/dev/null || true) &
fi
