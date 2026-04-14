#!/usr/bin/env bash
set -u

# Raw Claude OAuth test.
#
# This script bypasses OAuthModelRouter and calls Anthropic directly with the
# Claude Code OAuth credentials stored in macOS Keychain.
#
# It is intentionally non-mutating:
# - reads Keychain credentials
# - refreshes expired access tokens only in memory
# - never writes refreshed tokens back to Keychain
# - never writes to the router token database

DEFAULT_CLIENT_ID="${CLIENT_ID:-9d1c250a-e61b-44d9-88ed-5944d1962f5e}"
TOKEN_URL="${TOKEN_URL:-https://platform.claude.com/v1/oauth/token}"
API_URL="${CLAUDE_API_URL:-https://api.anthropic.com/v1/messages}"
MODEL="${CLAUDE_MODEL:-claude-haiku-4-5-20251001}"
MAX_TOKENS="${CLAUDE_MAX_TOKENS:-1}"
PROMPT="${CLAUDE_PROMPT:-hi}"
BETA_HEADER="${CLAUDE_BETA_HEADER:-oauth-2025-04-20}"
SCOPES="${CLAUDE_OAUTH_SCOPES:-user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload}"

SERVICES=(
  "Claude Code-credentials"
  "Claude Code-credentials-2"
)

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 2
  fi
}

json_get() {
  local key="$1"
  python3 -c '
import json
import sys

key = sys.argv[1]
data = json.load(sys.stdin)
oauth = data.get("claudeAiOauth", {})
value = oauth.get(key, "")
if isinstance(value, list):
    print(" ".join(str(item) for item in value))
elif value is None:
    print("")
else:
    print(value)
' "$key"
}

json_get_top_level() {
  local key="$1"
  python3 -c '
import json
import sys

key = sys.argv[1]
data = json.load(sys.stdin)
value = data.get(key, "")
if isinstance(value, list):
    print(" ".join(str(item) for item in value))
elif value is None:
    print("")
else:
    print(value)
' "$key"
}

token_suffix() {
  python3 - "$1" <<'PY'
import sys
token = sys.argv[1]
print("***" + token[-8:] if len(token) > 8 else "***")
PY
}

is_expired_ms() {
  local expires_at="$1"
  python3 - "$expires_at" <<'PY'
import sys
import time

raw = sys.argv[1]
try:
    expires_at = int(float(raw))
except Exception:
    print("unknown")
    raise SystemExit(0)

now = int(time.time() * 1000)
print("yes" if now >= expires_at else "no")
PY
}

refresh_access_token() {
  local service="$1"
  local refresh_token="$2"
  local client_id="$3"
  local scopes="$4"
  local body_file="$tmpdir/${service// /_}_refresh_body.json"
  local headers_file="$tmpdir/${service// /_}_refresh_headers.txt"

  echo "  refresh: access token expired; refreshing in memory" >&2

  local http_code
  http_code="$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST "$TOKEN_URL" \
      -H "content-type: application/x-www-form-urlencoded" \
      --data-urlencode "grant_type=refresh_token" \
      --data-urlencode "refresh_token=$refresh_token" \
      --data-urlencode "client_id=$client_id" \
      --data-urlencode "scope=$scopes"
  )"

  echo "  refresh_status: $http_code" >&2

  if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "  refresh_error:" >&2
    echo "  refresh_headers:" >&2
    sed -n '1,40p' "$headers_file" | sed 's/^/    /' >&2
    python3 -m json.tool "$body_file" >&2 2>/dev/null || sed -n '1,40p' "$body_file" >&2
    return 1
  fi

  python3 - "$body_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

access = data.get("access_token", "")
if not access:
    raise SystemExit(1)

print(access)
PY
}

call_messages_api() {
  local service="$1"
  local access_token="$2"
  local request_file="$tmpdir/${service// /_}_request.json"
  local body_file="$tmpdir/${service// /_}_body.json"
  local headers_file="$tmpdir/${service// /_}_headers.txt"

  python3 - "$MODEL" "$MAX_TOKENS" "$PROMPT" > "$request_file" <<'PY'
import json
import sys

model = sys.argv[1]
max_tokens = int(sys.argv[2])
prompt = sys.argv[3]

print(json.dumps({
    "model": model,
    "max_tokens": max_tokens,
    "messages": [{"role": "user", "content": prompt}],
}))
PY

  local http_code
  http_code="$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST "$API_URL" \
      -H "Authorization: Bearer $access_token" \
      -H "anthropic-version: 2023-06-01" \
      -H "anthropic-beta: $BETA_HEADER" \
      -H "content-type: application/json" \
      --data @"$request_file"
  )"

  echo "  api_status: $http_code"
  echo "  api_auth_shape: Authorization: Bearer <token>, anthropic-beta: $BETA_HEADER"

  echo "  rate_limit_headers:"
  grep -i "anthropic-ratelimit-unified" "$headers_file" | sed 's/^/    /' || echo "    none"

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "  result: PASS"
    python3 - "$body_file" <<'PY' 2>/dev/null || true
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

usage = data.get("usage")
if usage:
    print(f"  usage: input_tokens={usage.get('input_tokens')} output_tokens={usage.get('output_tokens')}")
PY
    return 0
  fi

  echo "  result: FAIL"
  echo "  response_body:"
  python3 -m json.tool "$body_file" 2>/dev/null | sed 's/^/    /' || sed -n '1,60p' "$body_file" | sed 's/^/    /'
  return 1
}

test_service() {
  local service="$1"
  echo
  echo "== $service =="

  local raw
  raw="$(security find-generic-password -s "$service" -a "$(whoami)" -w 2>/dev/null)"
  local security_status=$?

  if [[ "$security_status" -ne 0 || -z "$raw" ]]; then
    raw="$(security find-generic-password -s "$service" -w 2>/dev/null)"
    security_status=$?
    if [[ "$security_status" -ne 0 || -z "$raw" ]]; then
      echo "  status: SKIP"
      echo "  reason: keychain item not found or not readable"
      return 0
    fi
  fi

  local access_token refresh_token expires_at scopes subscription client_id
  access_token="$(printf '%s' "$raw" | json_get accessToken)"
  refresh_token="$(printf '%s' "$raw" | json_get refreshToken)"
  expires_at="$(printf '%s' "$raw" | json_get expiresAt)"
  scopes="$(printf '%s' "$raw" | json_get scopes)"
  subscription="$(printf '%s' "$raw" | json_get subscriptionType)"
  client_id="$(printf '%s' "$raw" | json_get_top_level oauthClientId)"
  client_id="${client_id:-$DEFAULT_CLIENT_ID}"
  scopes="${scopes:-$SCOPES}"

  if [[ -z "$access_token" ]]; then
    echo "  status: FAIL"
    echo "  reason: no claudeAiOauth.accessToken in keychain JSON"
    return 1
  fi

  echo "  plan: ${subscription:-unknown}"
  echo "  token: $(token_suffix "$access_token")"
  echo "  scopes: ${scopes:-unknown}"
  echo "  refresh_client_id: $client_id"
  echo "  expires_at_ms: ${expires_at:-unknown}"

  local expired
  expired="$(is_expired_ms "$expires_at")"
  echo "  expired: $expired"

  if [[ "$expired" == "yes" ]]; then
    if [[ -z "$refresh_token" ]]; then
      echo "  status: FAIL"
      echo "  reason: token expired and no refresh token exists"
      return 1
    fi

    local refreshed
    if ! refreshed="$(refresh_access_token "$service" "$refresh_token" "$client_id" "$scopes")"; then
      echo "  status: FAIL"
      echo "  reason: refresh failed"
      return 1
    fi
    access_token="$refreshed"
    echo "  refreshed_token: $(token_suffix "$access_token")"
  fi

  call_messages_api "$service" "$access_token"
}

main() {
  require_cmd curl
  require_cmd python3
  require_cmd security

  echo "Raw Claude OAuth Anthropic test"
  echo "api_url: $API_URL"
  echo "model: $MODEL"
  echo "max_tokens: $MAX_TOKENS"
  echo "beta_header: $BETA_HEADER"

  local service_list=("${SERVICES[@]}")
  if [[ "$#" -gt 0 ]]; then
    service_list=("$@")
  fi

  local failures=0
  local service
  for service in "${service_list[@]}"; do
    if ! test_service "$service"; then
      failures=$((failures + 1))
    fi
  done

  echo
  if [[ "$failures" -eq 0 ]]; then
    echo "Overall: PASS"
    return 0
  fi

  echo "Overall: FAIL ($failures service(s) failed)"
  return 1
}

main "$@"
