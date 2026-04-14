#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-8100}"
LOG_FILE="${LOG_FILE:-/tmp/oauthrouter-${PORT}.log}"

existing_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [[ -n "$existing_pid" ]]; then
  echo "Stopping existing server on port $PORT (pid $existing_pid)..."
  kill "$existing_pid" || true

  for _ in {1..20}; do
    if ! kill -0 "$existing_pid" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
fi

echo "Starting OAuthModelRouter on port $PORT..."
cd "$ROOT_DIR"
nohup env PYTHONPATH=src python3 -m oauthrouter.cli serve --port "$PORT" >"$LOG_FILE" 2>&1 &
new_pid="$!"

echo "Started pid $new_pid"
echo "Portal: http://127.0.0.1:${PORT}/portal"
echo "Log: $LOG_FILE"
