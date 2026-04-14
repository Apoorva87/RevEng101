# OpenAI OAuth Auth Flow (Codex CLI)

Reverse-engineered from the Codex CLI source (`codex-rs/login/src/server.rs`,
`codex-rs/login/src/pkce.rs`, `codex-rs/login/src/auth/manager.rs`) and
confirmed by testing against the live endpoints. April 2026.

## Endpoints

| Purpose | URL | Cloudflare? |
|---------|-----|-------------|
| OIDC Discovery | `https://auth.openai.com/.well-known/openid-configuration` | No |
| Authorize (browser) | `https://auth.openai.com/oauth/authorize` | No (rendered in browser) |
| Token (code exchange + refresh) | `https://auth.openai.com/oauth/token` | **No** |
| Token (Auth0 direct) | `https://auth0.openai.com/oauth/token` | **Yes** — blocks all non-browser clients (Cloudflare error 1000: "DNS points to prohibited IP") |
| Usage / rate limits | `https://chatgpt.com/backend-api/wham/usage` | Needs Bearer + ChatGPT-Account-Id |

**Key finding:** The OIDC discovery doc lists `auth0.openai.com/oauth/token` as the canonical
`token_endpoint`, but that endpoint is behind Cloudflare and blocks all programmatic access
(curl, Python urllib, Node.js fetch, Python requests, even curl_cffi with Chrome impersonation).
The Codex CLI uses `auth.openai.com/oauth/token` for everything — authorization code exchange,
token refresh, and token-for-token exchange. This endpoint has **no Cloudflare protection**.

## OAuth Client

```
client_id:  app_EMoamEEZ73f0CkXaXp7hrann
type:       Public (no client_secret — uses PKCE)
```

## Registered Redirect URI

```
http://localhost:1455/auth/callback
```

Only port **1455** works. Other ports return `AuthApiFailure / unknown_error`.
This port is hardcoded in the Codex OAuth client registration on OpenAI's side.

## Scopes

```
openid profile email offline_access api.connectors.read api.connectors.invoke
```

- `offline_access` -> gets a `refresh_token` in the response
- `api.connectors.read` / `api.connectors.invoke` -> Codex-specific API access

## Flow: PKCE Authorization Code

### Step 1 — Generate PKCE pair

```bash
# code_verifier: 64 random bytes -> URL-safe base64 without padding (87 chars)
# Codex uses 64 bytes; 43-128 chars is valid per RFC 7636
CODE_VERIFIER=$(openssl rand -base64 96 | tr -d '=+/\n' | head -c 128)

# code_challenge: base64url(sha256(code_verifier)), no padding
CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" \
  | openssl dgst -sha256 -binary \
  | openssl base64 -A \
  | tr '+/' '-_' | tr -d '=')
```

### Step 2 — Generate state (CSRF protection)

```bash
STATE=$(openssl rand -base64 32 | tr -d '=+/' | head -c 43)
```

### Step 3 — Start local callback server

Bind `http://127.0.0.1:1455` and wait for a GET to `/auth/callback?code=...&state=...`.
The server only captures the `code` parameter — token exchange is done separately.

### Step 4 — Open browser to authorize URL

```
https://auth.openai.com/oauth/authorize
  ?response_type=code
  &client_id=app_EMoamEEZ73f0CkXaXp7hrann
  &redirect_uri=http://localhost:1455/auth/callback
  &scope=openid profile email offline_access api.connectors.read api.connectors.invoke
  &audience=https://api.openai.com/v1
  &code_challenge={CODE_CHALLENGE}
  &code_challenge_method=S256
  &id_token_add_organizations=true
  &codex_cli_simplified_flow=true
  &state={STATE}
  &originator=codex-tui
```

Required params beyond standard OAuth:
- `id_token_add_organizations=true` — includes org info in the id_token
- `codex_cli_simplified_flow=true` — skips consent screen (already granted)
- `originator=codex-tui` — identifies the requesting app
- `audience=https://api.openai.com/v1` — the API the token is scoped to

Without `/oauth/authorize` (i.e., using just `/authorize` from OIDC discovery),
the page renders blank — the React SPA loads but fails silently.

### Step 5 — User logs in

Browser shows OpenAI login page. User signs in (Google, email, etc.).
On success, browser redirects to:

```
http://localhost:1455/auth/callback?code={AUTH_CODE}&state={STATE}
```

### Step 6 — Exchange code for tokens

**Endpoint: `https://auth.openai.com/oauth/token`** (NOT `auth0.openai.com`)

```bash
curl -X POST "https://auth.openai.com/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=app_EMoamEEZ73f0CkXaXp7hrann" \
  -d "code={AUTH_CODE}" \
  -d "redirect_uri=http://localhost:1455/auth/callback" \
  -d "code_verifier={CODE_VERIFIER}"
```

No special User-Agent or headers needed. Plain `curl` works.

Expected response (HTTP 200):
```json
{
  "access_token": "eyJ...",
  "refresh_token": "rt_...",
  "id_token": "eyJ...",
  "scope": "openid profile email offline_access ...",
  "expires_in": 864000,
  "token_type": "bearer"
}
```

### Step 7 — Decode JWT for account info

The `access_token` is a JWT. The payload contains:
```json
{
  "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
  "exp": 1776926387,
  "https://api.openai.com/auth": {
    "chatgpt_account_id": "944fe0d3-...",
    "chatgpt_plan_type": "plus",
    "chatgpt_user_id": "user-...",
    "localhost": true
  },
  "https://api.openai.com/profile": {
    "email": "user@gmail.com",
    "email_verified": true
  },
  "scp": ["openid", "profile", "email", "offline_access", ...]
}
```

### Step 8 — Refresh token (later)

**Note: Token refresh uses JSON body, not form-encoded.** This is different
from the authorization code exchange.

```bash
curl -X POST "https://auth.openai.com/oauth/token" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "grant_type": "refresh_token",
    "refresh_token": "{REFRESH_TOKEN}"
  }'
```

Refresh response includes new `access_token`, `refresh_token`, and `id_token`.

### Step 9 — Token exchange for API key (optional)

The Codex CLI can also exchange an id_token for an API-key-style token using
[RFC 8693 token exchange](https://www.rfc-editor.org/rfc/rfc8693):

```bash
curl -X POST "https://auth.openai.com/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "client_id=app_EMoamEEZ73f0CkXaXp7hrann" \
  -d "requested_token=openai-api-key" \
  -d "subject_token={ID_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:id_token"
```

**Note:** The `subject_token` (id_token) is single-use for this exchange.
A consumed or expired id_token returns `token_expired`.

## Token Lifetimes

- `access_token`: ~10 days (`expires_in: 864000`)
- `id_token`: ~1 hour (60 minutes)
- `refresh_token`: single-use (each refresh returns a new one)

## Device Code Flow (headless)

For environments without a browser, Codex supports device code auth:

1. **Get user code:** POST `https://auth.openai.com/api/accounts/deviceauth/usercode`
   with `{"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}`
2. **User visits URL:** Display the code, user enters it at OpenAI
3. **Poll for token:** POST `https://auth.openai.com/api/accounts/deviceauth/token`
   with `{device_auth_id, user_code}` at the specified interval
4. **Exchange:** On success, returns `{authorization_code, code_challenge, code_verifier}`
   which are exchanged via Step 6 above

---

## Cloudflare Problem & Solution

The OIDC discovery endpoint lists `auth0.openai.com/oauth/token` as the canonical
token endpoint. This domain is behind Cloudflare and returns HTTP 403 "DNS points
to prohibited IP" (error 1000) for **all** programmatic clients:

- `curl` (any User-Agent) -> 403
- Python `urllib` -> 403
- Python `requests` -> 403
- Python `httpx` -> 403
- Node.js `fetch` -> 403
- Python `curl_cffi` with Chrome TLS impersonation -> 403

**This is NOT a TLS fingerprinting issue.** Cloudflare error 1000 means the DNS
record points to a prohibited IP — no amount of TLS impersonation helps.

### Solution: Use `auth.openai.com` instead

The Codex CLI binary (confirmed via `strings` analysis and Rust source at
`codex-rs/login/src/server.rs:684-753`) uses `https://auth.openai.com/oauth/token`
for all token operations. This endpoint:

- Has **no Cloudflare protection**
- Accepts `grant_type=authorization_code` (with PKCE code_verifier)
- Accepts `grant_type=refresh_token` (with JSON body)
- Accepts `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`
- Works with plain `curl`, no special headers needed

### What went wrong during development

1. We initially tried `auth0.openai.com` (from OIDC discovery) -> Cloudflare 403
2. We tried `auth.openai.com` with `authorization_code` -> got `token_exchange_user_error`
   (this was likely an expired/reused code, not an endpoint rejection)
3. We tried browser-side `fetch()` -> CORS blocked
4. We tried Python `urllib` -> also 403 on `auth0.openai.com`
5. **Solution:** Read the Codex Rust source -> confirmed `auth.openai.com` with
   `authorization_code` grant is the correct approach -> works with plain `curl`

## Codex Source References

Key files in the Codex CLI source (`codex-rs/`):
- `login/src/server.rs:468-503` — Authorization URL construction
- `login/src/server.rs:684-753` — Code-for-token exchange (authorization_code grant)
- `login/src/server.rs:1059-1092` — Token exchange (id_token -> API key)
- `login/src/pkce.rs` — PKCE code_verifier/code_challenge generation
- `login/src/auth/manager.rs:667-708` — Token refresh logic
- `login/src/device_code_auth.rs` — Device code flow

## Credential Storage

The Codex CLI stores credentials in:
- **Primary:** OS keyring (macOS Keychain, Windows Credential Manager)
- **Fallback:** `~/.codex/.credentials.json` (600 permissions)
- **Legacy/simple:** `~/.codex/auth.json`
