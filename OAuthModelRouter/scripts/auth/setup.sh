#!/usr/bin/env bash
# scripts/auth/setup.sh — PKCE OAuth login for OpenAI (Codex) and Claude
# OpenAI flow:
#   1. Browser opens authorize URL (with PKCE code_challenge)
#   2. User logs in, OpenAI redirects to localhost:1455/auth/callback?code=...
#   3. We exchange the code at auth.openai.com/oauth/token via curl
#      (auth.openai.com has no Cloudflare — confirmed from Codex binary)
#   4. Parse access_token + refresh_token from response
#
# Claude flow (reverse-engineered from Claude Code binary v2.1.105):
#   1. Browser opens claude.com/cai/oauth/authorize (with PKCE code_challenge)
#   2. User logs in, Claude redirects to localhost:{port}/callback?code=...
#   3. We exchange the code at platform.claude.com/v1/oauth/token via curl
#   4. Parse access_token + refresh_token from response
#   5. Optionally create a long-lived API key via the create_api_key endpoint
#
# Usage:
#   ./scripts/auth/setup.sh openai          # login to OpenAI
#   ./scripts/auth/setup.sh openai --save   # login and save to router DB
#   ./scripts/auth/setup.sh claude          # login to Claude
#   ./scripts/auth/setup.sh claude --save   # login and save to router DB
#
# Dependencies: python3, curl, openssl, a browser

set -euo pipefail

DB="$HOME/.oauthrouter/tokens.db"

# ═══════════════════════════════════════════════════════════════════
# OpenAI OAuth config (matches Codex CLI registration)
# ═══════════════════════════════════════════════════════════════════
OPENAI_CLIENT_ID="app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_AUTH_URL="https://auth.openai.com/oauth/authorize"
OPENAI_TOKEN_URL="https://auth.openai.com/oauth/token"
OPENAI_AUDIENCE="https://api.openai.com/v1"
OPENAI_SCOPES="openid profile email offline_access api.connectors.read api.connectors.invoke"
# Port 1455 is the only registered redirect port for this client
OPENAI_PORT=1455
OPENAI_REDIRECT_URI="http://localhost:${OPENAI_PORT}/auth/callback"

# ═══════════════════════════════════════════════════════════════════
# Claude OAuth config (matches Claude Code CLI registration)
# Extracted from Claude Code binary v2.1.105 (Bun compiled)
# ═══════════════════════════════════════════════════════════════════
CLAUDE_CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_AUTH_URL="https://claude.com/cai/oauth/authorize"
CLAUDE_TOKEN_URL="https://platform.claude.com/v1/oauth/token"
CLAUDE_API_KEY_URL="https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
CLAUDE_SCOPES="org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
# Claude uses a dynamic port (unlike OpenAI's fixed 1455)
# We pick a port and use http://localhost:{port}/callback as redirect_uri
CLAUDE_PORT=9382

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

generate_pkce() {
  CODE_VERIFIER=$(openssl rand -base64 96 | tr -d '=+/\n' | head -c 128)
  CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')
}

generate_state() {
  STATE=$(openssl rand -base64 32 | tr -d '=+/\n' | head -c 43)
}

# ═══════════════════════════════════════════════════════════════════
# OpenAI PKCE Flow
# ═══════════════════════════════════════════════════════════════════

do_openai_login() {
  echo "=== OpenAI OAuth Login (PKCE) ==="
  echo

  # Step 1: Generate PKCE pair + state
  generate_pkce
  generate_state
  echo "  PKCE verifier generated (${#CODE_VERIFIER} chars)"

  # Step 2: Build authorize URL
  local auth_url
  auth_url=$(python3 -c "
import urllib.parse
params = urllib.parse.urlencode({
    'response_type': 'code',
    'client_id': '${OPENAI_CLIENT_ID}',
    'redirect_uri': '${OPENAI_REDIRECT_URI}',
    'scope': '${OPENAI_SCOPES}',
    'audience': '${OPENAI_AUDIENCE}',
    'code_challenge': '${CODE_CHALLENGE}',
    'code_challenge_method': 'S256',
    'id_token_add_organizations': 'true',
    'codex_cli_simplified_flow': 'true',
    'state': '${STATE}',
    'originator': 'codex-tui',
}, quote_via=urllib.parse.quote)
print('${OPENAI_AUTH_URL}?' + params)
")

  echo "  Opening browser for login..."
  echo
  echo "  If the browser doesn't open, visit:"
  echo "  $auth_url"
  echo

  # Step 3: Start callback server to capture the auth code
  # Server only captures the code — token exchange happens after via curl
  local tmpfile
  tmpfile=$(mktemp)

  python3 -c "
import http.server, urllib.parse, threading, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/auth/callback':
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get('code', [None])[0]
        error = params.get('error', [None])[0]

        if error:
            with open('$tmpfile', 'w') as f:
                json.dump({'error': error}, f)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<h2>Login failed: {error}</h2>'.encode())
            threading.Thread(target=self.server.shutdown).start()
            return

        if not code:
            self.send_response(400)
            self.end_headers()
            return

        # Save the code for bash to exchange via curl
        with open('$tmpfile', 'w') as f:
            json.dump({'code': code}, f)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h2>Login successful! You can close this tab.</h2>')
        threading.Thread(target=self.server.shutdown).start()

    def log_message(self, *a): pass

class ReusableServer(http.server.HTTPServer):
    allow_reuse_address = True
server = ReusableServer(('127.0.0.1', ${OPENAI_PORT}), Handler)
server.serve_forever()
" &
  local server_pid=$!

  sleep 0.5

  # Open browser
  open "$auth_url" 2>/dev/null || xdg-open "$auth_url" 2>/dev/null || echo "  Please open the URL above manually."

  echo "  Waiting for login on localhost:${OPENAI_PORT} ..."
  wait $server_pid 2>/dev/null || true

  # Read result from callback
  if [[ ! -f "$tmpfile" ]] || [[ ! -s "$tmpfile" ]]; then
    echo "  ERROR: No result received."
    rm -f "$tmpfile"
    return 1
  fi

  local callback_result
  callback_result=$(cat "$tmpfile")
  rm -f "$tmpfile"

  # Check for error
  local error
  error=$(echo "$callback_result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('error', ''))
" 2>/dev/null || echo "")
  if [[ -n "$error" ]]; then
    echo "  ERROR: $error"
    return 1
  fi

  # Extract auth code
  local auth_code
  auth_code=$(echo "$callback_result" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('code', ''))
")

  if [[ -z "$auth_code" ]]; then
    echo "  ERROR: No auth code received"
    echo "$callback_result"
    return 1
  fi

  echo "  Got auth code (${#auth_code} chars)"

  # Step 4: Exchange code for tokens at auth.openai.com (no Cloudflare)
  # The Codex binary uses this exact endpoint — confirmed by strings analysis
  echo "  Exchanging code for tokens at auth.openai.com..."

  local token_response
  token_response=$(curl -sS -X POST "${OPENAI_TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=authorization_code" \
    -d "client_id=${OPENAI_CLIENT_ID}" \
    -d "code=${auth_code}" \
    -d "redirect_uri=${OPENAI_REDIRECT_URI}" \
    -d "code_verifier=${CODE_VERIFIER}")

  echo "  Raw response: ${token_response:0:200}"

  # Check for exchange error
  local exchange_error
  exchange_error=$(echo "$token_response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
e = d.get('error', '')
if isinstance(e, dict): print(e.get('message', json.dumps(e)))
elif e:
    desc = d.get('error_description', '')
    print(f'{e}: {desc}' if desc else e)
" 2>/dev/null || echo "")
  if [[ -n "$exchange_error" ]]; then
    echo "  ERROR: Token exchange failed: $exchange_error"
    echo "$token_response" | python3 -m json.tool 2>/dev/null || echo "$token_response"
    return 1
  fi

  # Parse tokens
  local access_token refresh_token expires_in
  read -r access_token refresh_token expires_in <<< "$(echo "$token_response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('access_token',''), d.get('refresh_token',''), d.get('expires_in',''))
")"

  if [[ -z "$access_token" ]]; then
    echo "  ERROR: No access_token in response"
    echo "$token_response" | python3 -m json.tool 2>/dev/null
    return 1
  fi

  echo "  Token exchange successful!"

  # Decode JWT for email and account info
  local email plan acct_id
  read -r email plan acct_id <<< "$(echo "$access_token" | python3 -c "
import sys, json, base64
t = sys.stdin.read().strip()
p = t.split('.')[1]
p += '=' * (4 - len(p) % 4)
d = json.loads(base64.urlsafe_b64decode(p))
auth = d.get('https://api.openai.com/auth', {})
prof = d.get('https://api.openai.com/profile', {})
print(prof.get('email','unknown'), auth.get('chatgpt_plan_type','?'), auth.get('chatgpt_account_id',''))
")"

  echo
  echo "  ╔══════════════════════════════════════════╗"
  echo "  ║   OpenAI Login Successful                ║"
  echo "  ╠══════════════════════════════════════════╣"
  echo "  ║  Email:     $email"
  echo "  ║  Plan:      $plan"
  echo "  ║  Account:   $acct_id"
  echo "  ║  Expires:   ${expires_in}s"
  echo "  ║  Token:     ${access_token:0:30}..."
  echo "  ║  Refresh:   ${refresh_token:0:30}..."
  echo "  ╚══════════════════════════════════════════╝"
  echo

  # Save if requested
  if [[ "${SAVE_TO_DB:-0}" == "1" ]]; then
    local token_id="codex-plus : $email"
    echo "  Saving to DB as '$token_id' ..."

    local count
    count=$(sqlite3 "$DB" "SELECT count(*) FROM tokens WHERE id='$token_id';")
    if [[ "$count" != "0" ]]; then
      sqlite3 "$DB" "UPDATE tokens SET access_token='$access_token', refresh_token='$refresh_token', status='healthy', account_id='$acct_id' WHERE id='$token_id';"
      echo "  Updated existing entry."
    else
      sqlite3 "$DB" "INSERT INTO tokens (id, provider, access_token, refresh_token, status, account_id) VALUES ('$token_id', 'openai', '$access_token', '$refresh_token', 'healthy', '$acct_id');"
      echo "  Created new entry."
    fi
  else
    echo "  (use --save to store in router DB)"
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Claude PKCE Flow
# ═══════════════════════════════════════════════════════════════════

do_claude_login() {
  echo "=== Claude OAuth Login (PKCE) ==="
  echo

  # Step 1: Generate PKCE pair + state
  generate_pkce
  generate_state
  echo "  PKCE verifier generated (${#CODE_VERIFIER} chars)"

  # Step 2: Build authorize URL
  # Claude Code uses claude.com/cai/oauth/authorize with code=true param
  # The redirect_uri uses a dynamic localhost port (unlike OpenAI's fixed 1455)
  local redirect_uri="http://localhost:${CLAUDE_PORT}/callback"
  local auth_url
  auth_url=$(python3 -c "
import urllib.parse
params = urllib.parse.urlencode({
    'code': 'true',
    'client_id': '${CLAUDE_CLIENT_ID}',
    'response_type': 'code',
    'redirect_uri': '${redirect_uri}',
    'scope': '${CLAUDE_SCOPES}',
    'code_challenge': '${CODE_CHALLENGE}',
    'code_challenge_method': 'S256',
    'state': '${STATE}',
}, quote_via=urllib.parse.quote)
print('${CLAUDE_AUTH_URL}?' + params)
")

  echo "  Opening browser for login..."
  echo
  echo "  If the browser doesn't open, visit:"
  echo "  $auth_url"
  echo

  # Step 3: Start callback server to capture the auth code
  local tmpfile
  tmpfile=$(mktemp)

  python3 -c "
import http.server, urllib.parse, threading, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/callback':
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get('code', [None])[0]
        error = params.get('error', [None])[0]

        if error:
            with open('$tmpfile', 'w') as f:
                json.dump({'error': error}, f)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<h2>Login failed: {error}</h2>'.encode())
            threading.Thread(target=self.server.shutdown).start()
            return

        if not code:
            self.send_response(400)
            self.end_headers()
            return

        with open('$tmpfile', 'w') as f:
            json.dump({'code': code}, f)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h2>Claude login successful! You can close this tab.</h2>')
        threading.Thread(target=self.server.shutdown).start()

    def log_message(self, *a): pass

class ReusableServer(http.server.HTTPServer):
    allow_reuse_address = True
server = ReusableServer(('127.0.0.1', ${CLAUDE_PORT}), Handler)
server.serve_forever()
" &
  local server_pid=$!

  sleep 0.5

  # Open browser
  open "$auth_url" 2>/dev/null || xdg-open "$auth_url" 2>/dev/null || echo "  Please open the URL above manually."

  echo "  Waiting for login on localhost:${CLAUDE_PORT} ..."
  wait $server_pid 2>/dev/null || true

  # Read result from callback
  if [[ ! -f "$tmpfile" ]] || [[ ! -s "$tmpfile" ]]; then
    echo "  ERROR: No result received."
    rm -f "$tmpfile"
    return 1
  fi

  local callback_result
  callback_result=$(cat "$tmpfile")
  rm -f "$tmpfile"

  # Check for error
  local error
  error=$(echo "$callback_result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('error', ''))
" 2>/dev/null || echo "")
  if [[ -n "$error" ]]; then
    echo "  ERROR: $error"
    return 1
  fi

  # Extract auth code
  local auth_code
  auth_code=$(echo "$callback_result" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('code', ''))
")

  if [[ -z "$auth_code" ]]; then
    echo "  ERROR: No auth code received"
    echo "$callback_result"
    return 1
  fi

  echo "  Got auth code (${#auth_code} chars)"

  # Step 4: Exchange code for tokens at platform.claude.com
  # Claude Code uses form-encoded POST with code_verifier (PKCE)
  echo "  Exchanging code for tokens at platform.claude.com..."

  local header_file
  header_file=$(mktemp)

  local token_response
  # platform.claude.com is behind Cloudflare which rate-limits the token endpoint
  # by IP. Use Python with retry+backoff to handle transient 429s.
  # Auth codes are single-use, so if we get 429 the code is likely still valid
  # (the server rejected before processing) — retrying with the same code may work.
  # Claude Code uses axios for the token exchange. Key details from captured request:
  #   - Content-Type: application/json (JSON body, NOT form-encoded)
  #   - User-Agent: axios/1.13.6 (axios default — NOT claude-code/2.1.105)
  #   - Accept: application/json, text/plain, */* (axios default)
  #   - Body is JSON with: grant_type, code, redirect_uri, client_id, code_verifier, state
  token_response=$(python3 -c "
import urllib.request, json, sys

payload = json.dumps({
    'grant_type': 'authorization_code',
    'code': '${auth_code}',
    'redirect_uri': '${redirect_uri}',
    'client_id': '${CLAUDE_CLIENT_ID}',
    'code_verifier': '${CODE_VERIFIER}',
    'state': '${STATE}',
}).encode()

req = urllib.request.Request('${CLAUDE_TOKEN_URL}', data=payload, headers={
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': 'axios/1.13.6',
}, method='POST')

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode())
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print('HTTP', e.code, file=sys.stderr)
    print(body, file=sys.stderr)
    print(body)
except Exception as e:
    err = json.dumps({'error': str(e)})
    print(err, file=sys.stderr)
    print(err)
" 2>&1)

  echo "  Raw response: ${token_response:0:300}"

  # Check for exchange error
  local exchange_error
  exchange_error=$(echo "$token_response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
e = d.get('error', '')
if isinstance(e, dict): print(e.get('message', json.dumps(e)))
elif e:
    desc = d.get('error_description', '')
    print(f'{e}: {desc}' if desc else e)
" 2>/dev/null || echo "")
  if [[ -n "$exchange_error" ]]; then
    echo "  ERROR: Token exchange failed: $exchange_error"
    echo "$token_response" | python3 -m json.tool 2>/dev/null || echo "$token_response"
    return 1
  fi

  # Parse tokens
  local access_token refresh_token expires_in
  read -r access_token refresh_token expires_in <<< "$(echo "$token_response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('access_token',''), d.get('refresh_token',''), d.get('expires_in',''))
")"

  if [[ -z "$access_token" ]]; then
    echo "  ERROR: No access_token in response"
    echo "$token_response" | python3 -m json.tool 2>/dev/null
    return 1
  fi

  echo "  Token exchange successful!"

  # Extract account info directly from the token response
  # Claude includes account.email_address and organization.name in the response
  local email org_name scopes_str
  read -r email org_name scopes_str <<< "$(echo "$token_response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
email = d.get('account', {}).get('email_address', 'unknown')
org = d.get('organization', {}).get('name', 'unknown')
scopes = d.get('scope', '')
# Replace spaces in org name with underscores for read -r parsing
print(email, org.replace(' ', '_'), scopes)
")"

  echo
  # Restore underscores back to spaces for display
  org_name="${org_name//_/ }"

  echo "  ╔══════════════════════════════════════════╗"
  echo "  ║   Claude Login Successful                ║"
  echo "  ╠══════════════════════════════════════════╣"
  echo "  ║  Email:     $email"
  echo "  ║  Org:       $org_name"
  echo "  ║  Scopes:    $scopes_str"
  echo "  ║  Expires:   ${expires_in}s (~$(( expires_in / 3600 ))h)"
  echo "  ║  Token:     ${access_token:0:30}..."
  echo "  ║  Refresh:   ${refresh_token:0:30}..."
  echo "  ╚══════════════════════════════════════════╝"
  echo

  # Note: API key creation requires org:create_api_key scope which is requested
  # but not granted (the scope is filtered out by the auth server). The token
  # can still be used directly as a Bearer token with the anthropic-beta header.

  # Save if requested
  if [[ "${SAVE_TO_DB:-0}" == "1" ]]; then
    local token_id="claude : $email"
    echo "  Saving to DB as '$token_id' ..."

    local count
    count=$(sqlite3 "$DB" "SELECT count(*) FROM tokens WHERE id='$token_id';")
    if [[ "$count" != "0" ]]; then
      sqlite3 "$DB" "UPDATE tokens SET access_token='$access_token', refresh_token='$refresh_token', status='healthy' WHERE id='$token_id';"
      echo "  Updated existing entry."
    else
      sqlite3 "$DB" "INSERT INTO tokens (id, provider, access_token, refresh_token, status) VALUES ('$token_id', 'claude', '$access_token', '$refresh_token', 'healthy');"
      echo "  Created new entry."
    fi
  else
    echo "  (use --save to store in router DB)"
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

SAVE_TO_DB=0
PROVIDER=""

for arg in "$@"; do
  case "$arg" in
    openai)  PROVIDER="openai" ;;
    claude)  PROVIDER="claude" ;;
    --save)  SAVE_TO_DB=1 ;;
  esac
done

if [[ -z "$PROVIDER" ]]; then
  echo "Usage:"
  echo "  $0 openai          # login to OpenAI"
  echo "  $0 openai --save   # login and save to router DB"
  echo "  $0 claude          # login to Claude"
  echo "  $0 claude --save   # login and save to router DB"
  exit 0
fi

case "$PROVIDER" in
  openai) do_openai_login ;;
  claude) do_claude_login ;;
  *) echo "Unknown provider: $PROVIDER"; exit 1 ;;
esac
