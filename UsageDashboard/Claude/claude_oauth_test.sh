#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   export ACCESS_TOKEN="..."
#   export REFRESH_TOKEN="..."
#   export TOKEN_URL="https://YOUR_AUTH_SERVER/oauth/token"
#   ./claude_oauth_test.sh
#
# Optional overrides:
#   export CLIENT_ID="..."
#   export CLIENT_SECRET="..."
#   export CLAUDE_MODEL="claude-sonnet-4-6"
#   export CLAUDE_MAX_TOKENS="1024"
#   export CLAUDE_API_URL="https://api.anthropic.com/v1/messages"
#   export CLAUDE_PROMPT="Hello, Claude!"
#
# Notes:
# - This script does NOT extract tokens from any app.
# - You must supply your own OAuth tokens and token endpoint.

: "${ACCESS_TOKEN:?ACCESS_TOKEN is required}"
: "${REFRESH_TOKEN:?REFRESH_TOKEN is required}"
: "${TOKEN_URL:?TOKEN_URL is required}"

# Optional client auth, if your provider requires it for refresh.
CLIENT_ID="${CLIENT_ID:-}"
CLIENT_SECRET="${CLIENT_SECRET:-}"

# Claude API settings
API_URL="${CLAUDE_API_URL:-https://api.anthropic.com/v1/messages}"
MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"
MAX_TOKENS="${CLAUDE_MAX_TOKENS:-1024}"
PROMPT="${CLAUDE_PROMPT:-Hello! Please respond with a short greeting to confirm the API is working.}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# ── Call the Claude Messages API with a Bearer token ────────────────
call_api() {
  local token="$1"
  local body_file="$TMPDIR/api_body.json"
  local headers_file="$TMPDIR/api_headers.txt"
  local request_file="$TMPDIR/request.json"

  # Build the Messages API payload safely with jq
  if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required. Install with: brew install jq"
    return 1
  fi

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

  echo "==> Calling Claude API with OAuth access token..."
  echo "    Model:  $MODEL"
  echo "    Prompt: \"$PROMPT\""
  echo

  http_code=$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST "$API_URL" \
      -H "Authorization: Bearer $token" \
      -H "anthropic-version: 2023-06-01" \
      -H "Content-Type: application/json" \
      --data @"$request_file"
  )

  echo "HTTP status: $http_code"
  echo

  # ── Success ──
  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "--- Claude's response ---"
    jq -r '.content[] | select(.type == "text") | .text' "$body_file"
    echo
    echo "-------------------------"
    echo
    echo "--- Token usage ---"
    jq -r '"  Input tokens:  \(.usage.input_tokens)\n  Output tokens: \(.usage.output_tokens)"' "$body_file"
    echo
    echo "-------------------"
    return 0
  fi

  # ── Print the error body ──
  echo "--- Error response ---"
  jq '.' "$body_file" 2>/dev/null || cat "$body_file"
  echo
  echo "----------------------"

  # ── Detect token expiry (HTTP 401) ──
  if [[ "$http_code" == "401" ]]; then
    return 10  # signal: token expired / invalid → caller should refresh
  fi

  # ── Other errors ──
  case "$http_code" in
    400) echo "ERROR: Bad request — check model name and payload." ;;
    403) echo "ERROR: Permission denied — your OAuth scope may not cover this model." ;;
    404) echo "ERROR: Model '$MODEL' not found." ;;
    429) echo "ERROR: Rate limited."
         grep -i "retry-after" "$headers_file" 2>/dev/null || true ;;
    529) echo "ERROR: API is overloaded. Try again later." ;;
    *)   echo "ERROR: Unexpected status $http_code." ;;
  esac

  return 1
}

# ── Refresh the OAuth access token ──────────────────────────────────
refresh_access_token() {
  local body_file="$TMPDIR/refresh_body.json"
  local headers_file="$TMPDIR/refresh_headers.txt"

  echo "==> Refreshing access token via $TOKEN_URL ..."

  local curl_args=(
    -sS
    -D "$headers_file"
    -o "$body_file"
    -w "%{http_code}"
    -X POST "$TOKEN_URL"
    -H "Content-Type: application/x-www-form-urlencoded"
    --data-urlencode "grant_type=refresh_token"
    --data-urlencode "refresh_token=$REFRESH_TOKEN"
  )

  if [[ -n "$CLIENT_ID" ]]; then
    curl_args+=( --data-urlencode "client_id=$CLIENT_ID" )
  fi

  if [[ -n "$CLIENT_SECRET" ]]; then
    curl_args+=( --data-urlencode "client_secret=$CLIENT_SECRET" )
  fi

  http_code=$(curl "${curl_args[@]}")

  echo "HTTP status: $http_code"
  echo

  if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "--- Refresh error ---"
    jq '.' "$body_file" 2>/dev/null || cat "$body_file"
    echo
    echo "---------------------"
    echo "Refresh failed. You may need to re-authenticate."
    return 1
  fi

  NEW_ACCESS_TOKEN="$(jq -r '.access_token // empty' "$body_file")"
  NEW_REFRESH_TOKEN="$(jq -r '.refresh_token // empty' "$body_file")"

  if [[ -z "${NEW_ACCESS_TOKEN:-}" ]]; then
    echo "No access_token found in refresh response."
    echo "--- Refresh response ---"
    jq '.' "$body_file" 2>/dev/null || cat "$body_file"
    echo
    echo "------------------------"
    return 1
  fi

  ACCESS_TOKEN="$NEW_ACCESS_TOKEN"

  if [[ -n "${NEW_REFRESH_TOKEN:-}" ]]; then
    REFRESH_TOKEN="$NEW_REFRESH_TOKEN"
    echo "Refresh token rotated; using new refresh token."
  fi

  echo "Obtained new access token."
  echo
  return 0
}

# ── Main: try → refresh → retry ─────────────────────────────────────
main() {
  echo "============================================"
  echo "  Claude OAuth API Test"
  echo "  Model:      $MODEL"
  echo "  Max tokens: $MAX_TOKENS"
  echo "  API:        $API_URL"
  echo "  Token URL:  $TOKEN_URL"
  echo "============================================"
  echo

  # Attempt 1: call API with current access token
  call_api "$ACCESS_TOKEN"
  local status=$?

  if [[ "$status" -eq 0 ]]; then
    echo
    echo "Success with current access token."
    exit 0
  fi

  if [[ "$status" -ne 10 ]]; then
    echo
    echo "API call failed (not a token issue). See error above."
    exit 1
  fi

  # Token is expired/invalid — try refreshing
  echo
  echo "Access token appears expired or invalid (HTTP 401)."
  echo

  refresh_access_token

  # Attempt 2: retry with the refreshed token
  echo "==> Retrying with refreshed token..."
  echo

  if call_api "$ACCESS_TOKEN"; then
    echo
    echo "Success with refreshed access token."
    exit 0
  fi

  echo
  echo "Retry with refreshed token still failed."
  exit 1
}

main "$@"
