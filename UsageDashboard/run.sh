#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: run.sh <dashboard> [options]

Dashboards
----------
  unified       Local session viewer — reads ~/.claude and ~/.codex/sessions,
                shows active sessions, token usage timeline, and project breakdown.
                Port 8878  |  unified/unified_dashboard.py

  hub           Live API usage dashboard — tracks Claude and Codex/OpenAI
                account quota, billing, and rate limits via OAuth.
                Port 8765  |  usage_hub/usage_hub_web.py

  tokens        CLI token analyzer — scans ~/.claude projects and writes a
                token usage report + per-session prompts to ~/tuin/analysis/tokens/.
                (No web server — runs and exits)  |  token_analysis.py

  all           Launch unified + hub in parallel (tokens is CLI-only, run separately).

Options passed through to the chosen dashboard:
  --no-browser          Don't auto-open a browser tab
  --port PORT           Override the default port
  --host HOST           Override the default host (default: 127.0.0.1)

  unified only:
    --claude-root DIR         Claude root dir  (default: ~/.claude)
    --codex-sessions DIR      Codex sessions dir  (default: ~/.codex/sessions)
    --inactivity-days N       Days before marking session inactive  (default: 3)

  hub only:
    --config FILE             Config file  (default: .local/usage_hub.json)
    --no-initial-refresh      Skip refreshing accounts on startup

Examples
--------
  ./run.sh unified
  ./run.sh hub --no-browser
  ./run.sh tokens
  ./run.sh all --no-browser
  ./run.sh unified --port 9000 --claude-root ~/work/.claude
EOF
}

launch_unified() {
  echo "→ Starting unified session dashboard at http://127.0.0.1:8878/"
  python3 "$SCRIPT_DIR/unified/unified_dashboard.py" "$@"
}

launch_hub() {
  echo "→ Starting usage hub dashboard at http://127.0.0.1:8765/"
  python3 "$SCRIPT_DIR/usage_hub/usage_hub_web.py" "$@"
}

launch_tokens() {
  echo "→ Running token analysis..."
  python3 "$SCRIPT_DIR/token_analysis.py" "$@"
}

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

DASHBOARD="$1"
shift

case "$DASHBOARD" in
  unified)  launch_unified "$@" ;;
  hub)      launch_hub "$@" ;;
  tokens)   launch_tokens "$@" ;;
  all)
    launch_unified --no-browser "$@" &
    UNIFIED_PID=$!
    launch_hub --no-browser "$@" &
    HUB_PID=$!
    echo ""
    echo "Both dashboards running:"
    echo "  unified  →  http://127.0.0.1:8878/"
    echo "  hub      →  http://127.0.0.1:8765/"
    echo ""
    echo "Press Ctrl+C to stop both."
    trap "kill $UNIFIED_PID $HUB_PID 2>/dev/null; exit 0" INT TERM
    wait
    ;;
  *)
    echo "error: unknown dashboard '$DASHBOARD'"
    echo ""
    usage
    exit 1
    ;;
esac
