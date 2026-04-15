#!/usr/bin/env bash
# scripts/ops/db_tokens.sh — Interactive token DB manager
# One script for browsing, editing, adding, and health-checking tokens.
#
# Usage:
#   ./scripts/ops/db_tokens.sh          # interactive menu
#   ./scripts/ops/db_tokens.sh list     # non-interactive: list all
#   ./scripts/ops/db_tokens.sh check    # non-interactive: health check all
#   ./scripts/ops/db_tokens.sh --dry    # interactive, but health checks just print the curl
#   ./scripts/ops/db_tokens.sh check --dry  # print curl commands for all tokens

set -euo pipefail
DB="$HOME/.oauthrouter/tokens.db"
DRY=0
for arg in "$@"; do [[ "$arg" == "--dry" ]] && DRY=1; done

# ── Helpers ───────────────────────────────────────────────────────

mask_token_value() {
  local token="${1:-}"
  if [[ -z "$token" ]]; then
    echo ""
  elif [[ ${#token} -le 12 ]]; then
    echo "***"
  else
    echo "***${token: -12}"
  fi
}

print_field() {
  printf "    %-10s %s\n" "$1" "$2"
}

print_usage_header() {
  printf "    %-8s %-12s %-8s %s\n" "Window" "Status" "Used" "Reset"
}

print_usage_row() {
  printf "    %-8s %-12s %-8s %s\n" "$1" "$2" "$3" "$4"
}

list_all() {
  echo
  sqlite3 -header -column "$DB" \
    "SELECT id,
            provider,
            COALESCE(account_id, '-') AS account_id,
            status,
            priority,
            CASE
              WHEN access_token IS NULL OR access_token = '' THEN ''
              WHEN length(access_token) <= 12 THEN '***'
              ELSE '***' || substr(access_token, -12)
            END AS token_suffix,
            expires_at,
            last_used_at
     FROM tokens
     ORDER BY provider, id;"
  echo
}

pick_token() {
  local ids=()
  while IFS= read -r line; do ids+=("$line"); done < <(sqlite3 "$DB" "SELECT id FROM tokens ORDER BY provider, id;")

  if [[ ${#ids[@]} -eq 0 ]]; then
    echo "No tokens in DB."
    return 1
  fi

  echo
  for i in "${!ids[@]}"; do
    local p s
    p=$(sqlite3 "$DB" "SELECT provider FROM tokens WHERE id='${ids[$i]}';")
    s=$(sqlite3 "$DB" "SELECT status FROM tokens WHERE id='${ids[$i]}';")
    printf "  %d) [%s] %s (%s)\n" $((i+1)) "$p" "${ids[$i]}" "$s"
  done
  echo
  read -rp "Pick token [1-${#ids[@]}]: " choice
  if [[ -z "$choice" ]] || [[ "$choice" -lt 1 ]] || [[ "$choice" -gt ${#ids[@]} ]]; then
    echo "Invalid choice."
    return 1
  fi
  PICKED="${ids[$((choice-1))]}"
}

show_token() {
  local id="$1"
  echo
  echo "--- $id ---"
  sqlite3 -line "$DB" "SELECT * FROM tokens WHERE id='$id';"
  echo
}

edit_token() {
  local id="$1"
  local valid_fields=(id provider access_token refresh_token token_endpoint expires_at status last_used_at oauth_client_id scopes priority account_id)

  echo
  echo "Fields:"
  for i in "${!valid_fields[@]}"; do
    local val
    val=$(sqlite3 "$DB" "SELECT ${valid_fields[$i]} FROM tokens WHERE id='$id';")
    printf "  %2d) %-16s = %s\n" $((i+1)) "${valid_fields[$i]}" "${val:-(empty)}"
  done
  echo
  read -rp "Pick field [1-${#valid_fields[@]}]: " fchoice
  if [[ -z "$fchoice" ]] || [[ "$fchoice" -lt 1 ]] || [[ "$fchoice" -gt ${#valid_fields[@]} ]]; then
    echo "Invalid choice."
    return 1
  fi
  local field="${valid_fields[$((fchoice-1))]}"
  read -rp "New value for $field: " newval
  sqlite3 "$DB" "UPDATE tokens SET \"$field\"='$newval' WHERE id='$id';"
  echo "Updated."
  show_token "$id"
}

add_token() {
  echo
  echo "Known providers: claude, openai"
  read -rp "Provider: " prov
  read -rp "Account email/label: " acct
  local default_id="$prov : $acct"
  read -rp "Token ID [$default_id]: " tid
  [[ -z "$tid" ]] && tid="$default_id"
  read -rp "Access token / API key: " atoken

  local count
  count=$(sqlite3 "$DB" "SELECT count(*) FROM tokens WHERE id='$tid';")
  if [[ "$count" != "0" ]]; then
    echo "ERROR: '$tid' already exists. Use edit instead."
    return 1
  fi

  sqlite3 "$DB" "INSERT INTO tokens (id, provider, access_token, status, account_id) VALUES ('$tid', '$prov', '$atoken', 'healthy', '$acct');"
  echo "Added."
  show_token "$tid"
}

delete_token() {
  local id="$1"
  read -rp "Delete '$id'? (y/N): " confirm
  if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
    sqlite3 "$DB" "DELETE FROM tokens WHERE id='$id';"
    echo "Deleted."
  else
    echo "Cancelled."
  fi
}

healthcheck_one() {
  local id="$1"
  local provider access_token account_id
  provider=$(sqlite3 "$DB" "SELECT provider FROM tokens WHERE id='$id';")
  access_token=$(sqlite3 "$DB" "SELECT access_token FROM tokens WHERE id='$id';")
  account_id=$(sqlite3 "$DB" "SELECT COALESCE(account_id, '') FROM tokens WHERE id='$id';")
  local token_suffix
  token_suffix=$(mask_token_value "$access_token")

  echo "  == $id =="
  print_field "Provider" "$provider"
  if [[ -n "$account_id" ]]; then
    print_field "Account" "$account_id"
  fi
  print_field "Token" "$token_suffix"

  case "$provider" in
    claude)  healthcheck_claude "$id" "$access_token" ;;
    openai)  healthcheck_openai "$id" "$access_token" ;;
    *)
      echo "    SKIP: unknown provider '$provider'"
      ;;
  esac
  echo
}

healthcheck_claude() {
  local id="$1" token="$2"
  local token_suffix
  token_suffix=$(mask_token_value "$token")

  # Use Bearer + OAuth beta to get rate-limit headers back
  local curl_cmd="curl -sS -D - https://api.anthropic.com/v1/messages \
    -H 'Authorization: Bearer $token' \
    -H 'anthropic-version: 2023-06-01' \
    -H 'anthropic-beta: oauth-2025-04-20' \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"claude-haiku-4-5-20251001\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"

  if [[ "$DRY" == "1" ]]; then
    print_field "Dry run" "Claude auth probe"
    echo "    Command:"
    echo "      curl -sS -D - https://api.anthropic.com/v1/messages \\"
    echo "        -H 'Authorization: Bearer <redacted:${token_suffix}>' \\"
    echo "        -H 'anthropic-version: 2023-06-01' \\"
    echo "        -H 'anthropic-beta: oauth-2025-04-20' \\"
    echo "        -H 'Content-Type: application/json' \\"
    echo "        -d '{\"model\":\"claude-haiku-4-5-20251001\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
    return 0
  fi

  local full_response
  full_response=$(eval "$curl_cmd" 2>/dev/null)

  # Split headers and body
  local headers body http_code
  headers=$(printf '%s' "$full_response" | sed '/^\r$/q')
  http_code=$(printf '%s' "$headers" | head -1 | grep -oE '[0-9]{3}')

  # Parse rate-limit headers (present on both 200 and 429)
  local unified_status status_5h util_5h reset_5h status_7d util_7d reset_7d fallback
  unified_status=$(printf '%s' "$headers" | grep -i 'anthropic-ratelimit-unified-status:' | awk '{print $2}' | tr -d '\r' || true)
  status_5h=$(printf '%s' "$headers" | grep -i 'anthropic-ratelimit-unified-5h-status' | awk '{print $2}' | tr -d '\r' || true)
  util_5h=$(printf '%s' "$headers"   | grep -i 'anthropic-ratelimit-unified-5h-utilization' | awk '{print $2}' | tr -d '\r' || true)
  reset_5h=$(printf '%s' "$headers"  | grep -i 'anthropic-ratelimit-unified-5h-reset' | awk '{print $2}' | tr -d '\r' || true)
  status_7d=$(printf '%s' "$headers" | grep -i 'anthropic-ratelimit-unified-7d-status' | awk '{print $2}' | tr -d '\r' || true)
  util_7d=$(printf '%s' "$headers"   | grep -i 'anthropic-ratelimit-unified-7d-utilization' | awk '{print $2}' | tr -d '\r' || true)
  reset_7d=$(printf '%s' "$headers"  | grep -i 'anthropic-ratelimit-unified-7d-reset' | awk '{print $2}' | tr -d '\r' || true)
  fallback=$(printf '%s' "$headers"  | grep -i 'anthropic-ratelimit-unified-fallback:' | awk '{print $2}' | tr -d '\r' || true)

  # Determine health status
  print_field "HTTP" "${http_code:-?}"
  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    print_field "Result" "HEALTHY (auth ok, usage allowed)"
    sqlite3 "$DB" "UPDATE tokens SET status='healthy' WHERE id='$id';"
  elif [[ "$http_code" == "429" && -n "$unified_status" ]]; then
    # 429 with rate-limit headers = auth is good. Keep the DB token enabled;
    # the router handles cooldowns in memory instead of persisting a third state.
    print_field "Result" "RATE LIMITED (auth ok, token kept healthy in DB)"
    sqlite3 "$DB" "UPDATE tokens SET status='healthy' WHERE id='$id';"
  else
    # 401/403/other = auth is broken
    body=$(printf '%s' "$full_response" | sed '1,/^\r$/d')
    local err
    err=$(printf '%s' "$body" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin); e=d.get('error',{})
    print(e.get('message','') if isinstance(e,dict) else str(e))
except: print(sys.stdin.read()[:200])
" 2>/dev/null || echo "${body:0:200}")
    print_field "Error" "$err"
    print_field "Result" "AUTH FAILED (marked unhealthy)"
    sqlite3 "$DB" "UPDATE tokens SET status='unhealthy' WHERE id='$id';"
    return 0
  fi

  # Show usage (available on both 200 and 429)
  if [[ -z "$unified_status" ]]; then
    print_field "Usage" "No rate-limit headers returned"
    return 0
  fi

  # Convert utilization to percentage
  local pct_5h pct_7d
  pct_5h=$(python3 -c "print(int(float('${util_5h:-0}')*100))" 2>/dev/null || echo "?")
  pct_7d=$(python3 -c "print(int(float('${util_7d:-0}')*100))" 2>/dev/null || echo "?")

  # Convert reset timestamps to human-readable
  local reset_5h_str reset_7d_str
  reset_5h_str=$(python3 -c "import datetime; print(datetime.datetime.fromtimestamp(${reset_5h:-0}).strftime('%Y-%m-%d %H:%M'))" 2>/dev/null || echo "?")
  reset_7d_str=$(python3 -c "import datetime; print(datetime.datetime.fromtimestamp(${reset_7d:-0}).strftime('%Y-%m-%d %H:%M'))" 2>/dev/null || echo "?")

  print_usage_header
  print_usage_row "5h" "${status_5h:-?}" "${pct_5h}%" "$reset_5h_str"
  print_usage_row "7d" "${status_7d:-?}" "${pct_7d}%" "$reset_7d_str"
  print_field "Fallback" "${fallback:-?}"
}

healthcheck_openai() {
  local id="$1" token="$2"
  local token_suffix
  token_suffix=$(mask_token_value "$token")

  # Extract account_id from JWT payload
  local acct_id
  acct_id=$(printf '%s' "$token" | python3 -c "
import sys,json,base64
t=sys.stdin.read().strip()
p=t.split('.')[1]
p+='='*(4-len(p)%4)
print(json.loads(base64.urlsafe_b64decode(p))['https://api.openai.com/auth']['chatgpt_account_id'])
" 2>/dev/null)

  if [[ -z "$acct_id" ]]; then
    print_field "Error" "Could not extract account_id from JWT"
    return 1
  fi

  local curl_cmd="curl -sS 'https://chatgpt.com/backend-api/wham/usage' \
    -H 'Authorization: Bearer $token' \
    -H 'ChatGPT-Account-Id: $acct_id' \
    -H 'Accept: application/json'"

  if [[ "$DRY" == "1" ]]; then
    print_field "Dry run" "OpenAI usage probe"
    echo "    Command:"
    echo "      curl -sS 'https://chatgpt.com/backend-api/wham/usage' \\"
    echo "        -H 'Authorization: Bearer <redacted:${token_suffix}>' \\"
    echo "        -H 'ChatGPT-Account-Id: $acct_id' \\"
    echo "        -H 'Accept: application/json'"
    return 0
  fi

  local response
  response=$(eval "$curl_cmd" 2>/dev/null)

  # Parse the response
  local parsed
  parsed=$(printf '%s' "$response" | python3 -c "
import sys, json, datetime

def fmt_pct(value):
    if value in (None, ''):
        return '?'
    try:
        return f'{float(value):.0f}%'
    except Exception:
        return f'{value}%'

def fmt_reset(value):
    if not value:
        return '?'
    try:
        return datetime.datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '?'

try:
    d = json.load(sys.stdin)
    rl = d.get('rate_limit', {})
    pw = rl.get('primary_window', {})
    sw = rl.get('secondary_window', {})
    print('OK')
    print('\t'.join([
        str(d.get('plan_type', '?')),
        str(d.get('email', '?')),
        str(rl.get('allowed', '?')),
        fmt_pct(pw.get('used_percent')),
        fmt_reset(pw.get('reset_at')),
        fmt_pct(sw.get('used_percent')),
        fmt_reset(sw.get('reset_at')),
    ]))
except Exception as e:
    print('FAIL')
    print(str(e))
" 2>/dev/null)

  local parse_status parse_payload
  parse_status=$(printf '%s\n' "$parsed" | sed -n '1p')
  parse_payload=$(printf '%s\n' "$parsed" | sed -n '2p')

  if [[ "$parse_status" == "OK" ]]; then
    local plan email allowed pw_pct pw_reset sw_pct sw_reset
    IFS=$'\t' read -r plan email allowed pw_pct pw_reset sw_pct sw_reset <<< "$parse_payload"
    print_field "Result" "HEALTHY"
    print_field "Plan" "$plan"
    print_field "Email" "$email"
    print_field "Allowed" "$allowed"
    print_usage_header
    print_usage_row "5h" "-" "$pw_pct" "$pw_reset"
    print_usage_row "7d" "-" "$sw_pct" "$sw_reset"
    sqlite3 "$DB" "UPDATE tokens SET status='healthy' WHERE id='$id';"
  else
    print_field "Error" "${parse_payload:-Could not parse response}"
    print_field "Result" "FAILED (marked unhealthy)"
    sqlite3 "$DB" "UPDATE tokens SET status='unhealthy' WHERE id='$id';"
  fi
}

healthcheck_all() {
  echo
  while IFS= read -r id; do
    healthcheck_one "$id"
  done < <(sqlite3 "$DB" "SELECT id FROM tokens;")
}

# ── Non-interactive shortcuts ─────────────────────────────────────
if [[ "${1:-}" == "list" ]]; then list_all; exit 0; fi
if [[ "${1:-}" == "check" ]]; then healthcheck_all; exit 0; fi

# ── Interactive menu loop ─────────────────────────────────────────
while true; do
  echo "═══════════════════════════════════════"
  echo "  Token DB Manager  ($DB)"
  echo "═══════════════════════════════════════"
  echo "  1) List all tokens"
  echo "  2) Show token details"
  echo "  3) Edit a token field"
  echo "  4) Add new token"
  echo "  5) Delete a token"
  echo "  6) Health check one token"
  echo "  7) Health check ALL tokens"
  echo "  q) Quit"
  echo
  read -rp "> " action

  case "$action" in
    1) list_all ;;
    2) pick_token && show_token "$PICKED" ;;
    3) pick_token && edit_token "$PICKED" ;;
    4) add_token ;;
    5) pick_token && delete_token "$PICKED" ;;
    6) pick_token && healthcheck_one "$PICKED" ;;
    7) healthcheck_all ;;
    q|Q) echo "Bye."; exit 0 ;;
    *) echo "Invalid choice." ;;
  esac
done
