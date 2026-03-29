#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   ./claude_api_test.sh
#
# Optional overrides:
#   export CLAUDE_MODEL="claude-sonnet-4-6"      # default: claude-sonnet-4-6
#   export CLAUDE_MAX_TOKENS="1024"               # default: 1024
#   export CLAUDE_API_URL="https://api.anthropic.com/v1/messages"
#   export CLAUDE_PROMPT="Hello, Claude!"

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required — get one at console.anthropic.com}"

API_URL="${CLAUDE_API_URL:-https://api.anthropic.com/v1/messages}"
MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"
MAX_TOKENS="${CLAUDE_MAX_TOKENS:-1024}"
PROMPT="${CLAUDE_PROMPT:-Hello! Please respond with a short greeting to confirm the API is working.}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# ── Step 1: Verify the API key works ────────────────────────────────
verify_api_key() {
  local body_file="$TMPDIR/verify_body.json"
  local headers_file="$TMPDIR/verify_headers.txt"

  echo "==> Verifying API key..."
  http_code=$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X GET "https://api.anthropic.com/v1/models" \
      -H "x-api-key: $ANTHROPIC_API_KEY" \
      -H "anthropic-version: 2023-06-01"
  )

  echo "HTTP status: $http_code"

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "API key is valid."
    echo
    return 0
  fi

  echo "--- Response body ---"
  cat "$body_file"
  echo
  echo "---------------------"

  if [[ "$http_code" == "401" ]]; then
    echo "ERROR: Invalid API key. Check your ANTHROPIC_API_KEY."
    return 1
  elif [[ "$http_code" == "403" ]]; then
    echo "ERROR: API key lacks permissions. Check console.anthropic.com."
    return 1
  else
    echo "ERROR: Unexpected status $http_code during key verification."
    return 1
  fi
}

# ── Step 2: Send a message to the Claude API ────────────────────────
send_message() {
  local body_file="$TMPDIR/msg_body.json"
  local headers_file="$TMPDIR/msg_headers.txt"
  local request_file="$TMPDIR/request.json"

  # Build the JSON payload safely (handles special characters in PROMPT)
  if command -v jq >/dev/null 2>&1; then
    jq -n \
      --arg model "$MODEL" \
      --argjson max_tokens "$MAX_TOKENS" \
      --arg prompt "$PROMPT" \
      '{
        model: $model,
        max_tokens: $max_tokens,
        messages: [
          { role: "user", content: $prompt }
        ]
      }' > "$request_file"
  else
    echo "jq is required to safely build JSON payloads."
    echo "Install with: brew install jq"
    return 1
  fi

  echo "==> Sending message to Claude ($MODEL)..."
  echo "    Prompt: \"$PROMPT\""
  echo

  http_code=$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST "$API_URL" \
      -H "x-api-key: $ANTHROPIC_API_KEY" \
      -H "anthropic-version: 2023-06-01" \
      -H "Content-Type: application/json" \
      --data @"$request_file"
  )

  echo "HTTP status: $http_code"
  echo

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "--- Claude's response ---"
    # Extract the text content from the response
    jq -r '.content[] | select(.type == "text") | .text' "$body_file"
    echo
    echo "-------------------------"
    echo

    # Print usage stats
    echo "--- Token usage ---"
    jq -r '"  Input tokens:  \(.usage.input_tokens)\n  Output tokens: \(.usage.output_tokens)"' "$body_file"
    echo
    echo "-------------------"
    return 0
  fi

  echo "--- Error response ---"
  jq '.' "$body_file" 2>/dev/null || cat "$body_file"
  echo
  echo "----------------------"

  case "$http_code" in
    400) echo "ERROR: Bad request — check model name and parameters." ;;
    401) echo "ERROR: Authentication failed." ;;
    403) echo "ERROR: Permission denied for this model or feature." ;;
    404) echo "ERROR: Model '$MODEL' not found. Check the model ID." ;;
    429) echo "ERROR: Rate limited. Wait and retry."
         # Show retry-after header if present
         grep -i "retry-after" "$headers_file" 2>/dev/null || true ;;
    529) echo "ERROR: API is overloaded. Try again later." ;;
    *)   echo "ERROR: Unexpected status $http_code." ;;
  esac

  return 1
}

# ── Main ────────────────────────────────────────────────────────────
main() {
  echo "============================================"
  echo "  Claude API Test"
  echo "  Model:      $MODEL"
  echo "  Max tokens: $MAX_TOKENS"
  echo "  Endpoint:   $API_URL"
  echo "============================================"
  echo

  verify_api_key
  send_message

  echo
  echo "Done. API is working correctly."
}

main "$@"
