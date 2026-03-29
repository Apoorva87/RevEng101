#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_OUTPUT="$SCRIPT_DIR/.local/accounts.ini"
AUTH_FILE="${CODEX_AUTH_FILE:-$HOME/.codex/auth.json}"
SESSIONS_DIR="${CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}"
AUTH_FILE_ENTRY="${CODEX_AUTH_FILE_ENTRY:-$AUTH_FILE}"
ACCOUNT_NAME="${1:-local}"
OUTPUT_PATH="${2:-$DEFAULT_OUTPUT}"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required but was not found in PATH." >&2
  exit 1
fi

if [[ ! -f "$AUTH_FILE" ]]; then
  echo "Codex auth file not found at: $AUTH_FILE" >&2
  echo "Set CODEX_AUTH_FILE if your auth.json lives elsewhere." >&2
  exit 1
fi

ACCESS_TOKEN="$(jq -r '.tokens.access_token // empty' "$AUTH_FILE")"
ACCOUNT_ID="$(jq -r '.tokens.account_id // empty' "$AUTH_FILE")"

if [[ -z "$ACCESS_TOKEN" || -z "$ACCOUNT_ID" ]]; then
  echo "Could not read access_token/account_id from: $AUTH_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

cat >"$OUTPUT_PATH" <<EOF
[$ACCOUNT_NAME]
access_token=$ACCESS_TOKEN
account_id=$ACCOUNT_ID
sessions_dir=$SESSIONS_DIR
auth_file=$AUTH_FILE_ENTRY
EOF

echo "Wrote $OUTPUT_PATH for account [$ACCOUNT_NAME]"
echo "Run: python3 \"$SCRIPT_DIR/codex_nerve.py\" --config \"$OUTPUT_PATH\""
