#!/usr/bin/env bash
set -euo pipefail

# Direct token test — bypasses the proxy entirely.
# Reads tokens from the OAuthModelRouter SQLite DB.
#
# Usage:
#   ./scripts/debug/test_tokens.sh              # test both tokens
#   ./scripts/debug/test_tokens.sh refresh 1    # refresh credential-1
#   ./scripts/debug/test_tokens.sh refresh 2    # refresh credential-2

DB="$HOME/.oauthrouter/tokens.db"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
API_URL="https://api.anthropic.com/v1/messages"

# ── Read tokens from DB ───────────────────────────────────────────
read_token() {
  local name="$1"
  sqlite3 "$DB" "SELECT access_token FROM tokens WHERE id='$name';"
}

read_refresh_token() {
  local name="$1"
  sqlite3 "$DB" "SELECT refresh_token FROM tokens WHERE id='$name';"
}

read_client_id() {
  local name="$1"
  sqlite3 "$DB" "SELECT oauth_client_id FROM tokens WHERE id='$name';"
}

read_scopes() {
  local name="$1"
  sqlite3 "$DB" "SELECT scopes FROM tokens WHERE id='$name';"
}

CRED1_NAME="claude code-credentials"
CRED2_NAME="claude code-credentials-2"

# ── Test a single token ────────────────────────────────────────────
test_token() {
  local label="$1"
  local token="$2"

  echo "=== Testing: $label ==="
  echo "    Token: ${token:0:20}...${token: -10}"
  echo

  local body
  body=$(curl -sS -w "\n%{http_code}" \
    -X POST "$API_URL" \
    -H "Authorization: Bearer $token" \
    -H "anthropic-version: 2023-06-01" \
    -H "anthropic-beta: oauth-2025-04-20" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-opus-4-6","max_tokens":30,"messages":[{"role":"user","content":"Say exactly: token test OK"}]}')

  local http_code
  http_code=$(echo "$body" | tail -1)
  local response
  response=$(echo "$body" | sed '$d')

  echo "    HTTP: $http_code"

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    local text
    text=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['content'][0]['text'])" 2>/dev/null || echo "$response")
    echo "    Response: $text"
    echo "    Result: OK"
    echo "$response"
  else
    local err
    err=$(echo "$response" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    e=d.get('error',{})
    print(e.get('message','') if isinstance(e,dict) else e)
except: print(sys.stdin.read())
" 2>/dev/null || echo "$response")
    echo "    Error: $err"
    echo "    Result: FAILED"
  fi
  echo
}

# ── Refresh a token ───────────────────────────────────────────────
refresh_token() {
  local label="$1"
  local name="$2"

  local rt
  rt=$(read_refresh_token "$name")
  local client_id
  client_id=$(read_client_id "$name")
  local scopes
  scopes=$(read_scopes "$name")

  echo "=== Refreshing: $label ==="
  echo "    Refresh token: ${rt:0:20}...${rt: -10}"
  echo "    Client ID: ${client_id:-NONE}"
  echo "    Scopes: ${scopes:-NONE}"

  if [[ -z "$rt" ]]; then
    echo "    ERROR: No refresh token available."
    return 1
  fi

  # Build POST data
  local post_data="grant_type=refresh_token&refresh_token=$rt"
  if [[ -n "$client_id" ]]; then
    post_data="$post_data&client_id=$client_id"
  fi
  if [[ -n "$scopes" ]]; then
    local encoded_scopes
    encoded_scopes=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$scopes'))")
    post_data="$post_data&scope=$encoded_scopes"
  fi

  echo "    Endpoint: $TOKEN_URL"
  echo

  local body
  body=$(curl -sS -w "\n%{http_code}" \
    -X POST "$TOKEN_URL" \
    -H "Content-Type: application/x-www-form-urlencoded;charset=UTF-8" \
    -d "$post_data")

  local http_code
  http_code=$(echo "$body" | tail -1)
  local response
  response=$(echo "$body" | sed '$d')

  echo "    HTTP: $http_code"

  if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "    ERROR: Refresh failed"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
    return 1
  fi

  # Parse new tokens
  local new_at new_rt expires_in
  new_at=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  new_rt=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))")
  expires_in=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_in','?'))")

  echo "    New access token: ${new_at:0:20}...${new_at: -10}"
  echo "    New refresh token: ${new_rt:0:20}...${new_rt: -10}"
  echo "    Expires in: ${expires_in}s"

  # Update the DB
  sqlite3 "$DB" "UPDATE tokens SET access_token='$new_at', refresh_token='$new_rt', status='healthy' WHERE id='$name';"

  # Compute new expires_at
  python3 -c "
import sqlite3, datetime
conn = sqlite3.connect('$DB')
exp = datetime.datetime.utcnow() + datetime.timedelta(seconds=$expires_in)
conn.execute('UPDATE tokens SET expires_at=? WHERE id=?', (exp.isoformat(), '$name'))
conn.commit()
"

  echo "    DB updated."
  echo "    Result: OK"
  echo
}

# ── Main ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "refresh" ]]; then
  case "${2:-}" in
    1) refresh_token "Credential 1" "$CRED1_NAME" ;;
    2) refresh_token "Credential 2" "$CRED2_NAME" ;;
    *)
      echo "Usage: $0 refresh [1|2]"
      echo "  1 = $CRED1_NAME"
      echo "  2 = $CRED2_NAME"
      exit 1
      ;;
  esac
  exit 0
fi

echo "╔══════════════════════════════════════════╗"
echo "║   Direct Token Test (no proxy)           ║"
echo "╠══════════════════════════════════════════╣"
echo "║   API: $API_URL"
echo "║   DB:  $DB"
echo "╚══════════════════════════════════════════╝"
echo

AT1=$(read_token "$CRED1_NAME")
AT2=$(read_token "$CRED2_NAME")

test_token "Credential 1 ($CRED1_NAME)" "$AT1"
test_token "Credential 2 ($CRED2_NAME)" "$AT2"

echo "Done. To refresh a token:"
echo "  $0 refresh 1    # refresh credential-1"
echo "  $0 refresh 2    # refresh credential-2"
