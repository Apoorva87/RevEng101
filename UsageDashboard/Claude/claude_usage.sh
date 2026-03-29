#!/bin/bash
# claude_usage.sh — Check Claude Pro/Max rate limit usage
# Reads OAuth token from macOS Keychain (set by Claude Code)
# Automatically refreshes expired tokens

set -euo pipefail

CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
SCOPES="user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload"

# ── Read credentials from Keychain ──────────────────────────────
CREDS_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w)
TOKEN=$(echo "$CREDS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['accessToken'])")
REFRESH_TOKEN=$(echo "$CREDS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['refreshToken'])")
EXPIRES_AT=$(echo "$CREDS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['expiresAt'])")

# ── Refresh token if expired ────────────────────────────────────
NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")
if [ "$NOW_MS" -ge "$EXPIRES_AT" ]; then
  echo "Token expired. Refreshing..."
  ENCODED_SCOPES=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$SCOPES'))")
  REFRESH_RESP=$(curl -s "$TOKEN_URL" \
    -H "content-type: application/x-www-form-urlencoded" \
    -d "grant_type=refresh_token&refresh_token=$REFRESH_TOKEN&client_id=$CLIENT_ID&scope=$ENCODED_SCOPES")

  # Check for error
  if echo "$REFRESH_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'access_token' in d else 1)" 2>/dev/null; then
    NEW_ACCESS=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
    NEW_REFRESH=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))")
    EXPIRES_IN=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_in',3600))")
    NEW_EXPIRES_AT=$(python3 -c "import time; print(int(time.time()*1000 + $EXPIRES_IN*1000))")

    # Update the keychain with new tokens
    UPDATED_JSON=$(echo "$CREDS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['claudeAiOauth']['accessToken'] = '$NEW_ACCESS'
if '$NEW_REFRESH': d['claudeAiOauth']['refreshToken'] = '$NEW_REFRESH'
d['claudeAiOauth']['expiresAt'] = $NEW_EXPIRES_AT
print(json.dumps(d))
")
    security delete-generic-password -s "Claude Code-credentials" -a "$(whoami)" >/dev/null 2>&1 || true
    security add-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w "$UPDATED_JSON"
    TOKEN="$NEW_ACCESS"
    echo "Token refreshed successfully."
  else
    echo "ERROR: Token refresh failed:"
    echo "$REFRESH_RESP" | python3 -m json.tool 2>/dev/null || echo "$REFRESH_RESP"
    exit 1
  fi
fi

# ── Call API and read rate limit headers ────────────────────────
HEADERS=$(curl -s -D - -o /dev/null https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' 2>/dev/null)

# Check if API call succeeded
HTTP_STATUS=$(echo "$HEADERS" | head -1 | awk '{print $2}')
if [ "$HTTP_STATUS" = "401" ]; then
  echo "ERROR: Authentication failed (401). Token may be invalid."
  echo "Try running: claude /login"
  exit 1
fi

STATUS=$(echo "$HEADERS" | grep "unified-status:" | head -1 | awk '{print $2}' | tr -d '\r')
UTIL_5H=$(echo "$HEADERS" | grep "unified-5h-utilization:" | awk '{print $2}' | tr -d '\r')
UTIL_7D=$(echo "$HEADERS" | grep "unified-7d-utilization:" | awk '{print $2}' | tr -d '\r')
RESET_5H=$(echo "$HEADERS" | grep "unified-5h-reset:" | awk '{print $2}' | tr -d '\r')
RESET_7D=$(echo "$HEADERS" | grep "unified-7d-reset:" | awk '{print $2}' | tr -d '\r')
STATUS_5H=$(echo "$HEADERS" | grep "unified-5h-status:" | awk '{print $2}' | tr -d '\r')
STATUS_7D=$(echo "$HEADERS" | grep "unified-7d-status:" | awk '{print $2}' | tr -d '\r')
OVERAGE=$(echo "$HEADERS" | grep "unified-overage-status:" | awk '{print $2}' | tr -d '\r')

PCT_5H=$(python3 -c "print(f'{float(${UTIL_5H:-0}) * 100:.0f}%')")
PCT_7D=$(python3 -c "print(f'{float(${UTIL_7D:-0}) * 100:.0f}%')")
REM_5H=$(python3 -c "print(f'{(1 - float(${UTIL_5H:-0})) * 100:.0f}%')")
REM_7D=$(python3 -c "print(f'{(1 - float(${UTIL_7D:-0})) * 100:.0f}%')")
RESET_5H_FMT=$(date -r "${RESET_5H:-0}" "+%a %b %d %I:%M %p %Z" 2>/dev/null || echo "unknown")
RESET_7D_FMT=$(date -r "${RESET_7D:-0}" "+%a %b %d %I:%M %p %Z" 2>/dev/null || echo "unknown")

echo "╔══════════════════════════════════════════╗"
echo "║        Claude Pro Usage Dashboard        ║"
echo "╠══════════════════════════════════════════╣"
printf "║  Status:          %-22s║\n" "$STATUS"
echo "║──────────────────────────────────────────║"
printf "║  5-Hour Window:   %-22s║\n" "$STATUS_5H"
printf "║    Used:          %-22s║\n" "$PCT_5H"
printf "║    Remaining:     %-22s║\n" "$REM_5H"
printf "║    Resets:        %-22s║\n" "$RESET_5H_FMT"
echo "║──────────────────────────────────────────║"
printf "║  7-Day Window:    %-22s║\n" "$STATUS_7D"
printf "║    Used:          %-22s║\n" "$PCT_7D"
printf "║    Remaining:     %-22s║\n" "$REM_7D"
printf "║    Resets:        %-22s║\n" "$RESET_7D_FMT"
echo "║──────────────────────────────────────────║"
printf "║  Overage:         %-22s║\n" "$OVERAGE"
echo "╚══════════════════════════════════════════╝"
