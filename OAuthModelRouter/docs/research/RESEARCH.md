# OAuthModelRouter Research Notes

These notes summarize Claude OAuth reverse-engineering material from an earlier
local research workspace, including:

- a notes directory with Claude OAuth findings
- a usage-probe script
- a Claude OAuth test script
- a generic OAuth test script

The goal is to capture the critical facts needed to make this router work with
Claude Code / Claude Pro / Claude Max OAuth credentials.

## Core Finding

Claude Pro/Max credentials from Claude Code are OAuth credentials, not API keys.

For upstream Anthropic API requests, the access token must be sent as:

```http
Authorization: Bearer <access_token>
```

It must not be sent as:

```http
x-api-key: <access_token>
```

The OAuth token is only accepted by Anthropic's API when the OAuth beta header is
present:

```http
anthropic-beta: oauth-2025-04-20
```

Some Claude Code binary analysis found this fuller beta value:

```http
anthropic-beta: files-api-2025-04-14,oauth-2025-04-20
```

For normal `/v1/messages` calls, the minimal required OAuth shape is:

```http
Authorization: Bearer <access_token>
anthropic-version: 2023-06-01
anthropic-beta: oauth-2025-04-20
content-type: application/json
```

If the OAuth beta header is missing, Anthropic can return:

```text
401 OAuth authentication is currently not supported
```

That error does not necessarily mean the access token is bad. It usually means
the request used Bearer auth without the required beta header.

## Why Claude Code Fails Before The Router

When Claude Code is run with only a base URL override:

```bash
ANTHROPIC_BASE_URL=http://localhost:8100/claude claude -p "hello"
```

the client can fail locally with:

```text
Not logged in - Please run /login
```

In that case, the router never receives a useful request. Claude Code still
expects some local auth material before it will send API traffic.

For router testing, a placeholder key can be used to get Claude Code to make the
request:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8100/claude \
ANTHROPIC_API_KEY=oauthrouter \
claude --bare -p "hello"
```

The router must treat that `ANTHROPIC_API_KEY` value as a client-side placeholder
only. Before forwarding upstream, the router should strip incoming auth headers
such as:

```http
authorization
x-api-key
api-key
```

Then it should inject the selected account's OAuth access token as:

```http
Authorization: Bearer <selected_access_token>
```

## Required Router Behavior For Claude

For provider `claude`, a routed upstream request should:

1. Select a healthy Claude token/account.
2. Strip the caller's placeholder auth headers.
3. Inject `Authorization: Bearer <access_token>`.
4. Ensure `anthropic-version: 2023-06-01` is present.
5. Ensure `anthropic-beta` contains `oauth-2025-04-20`.
6. Forward the original request body to `https://api.anthropic.com/<path>`.
7. If upstream returns `401` or `403`, refresh the token and retry or fail over.
8. If upstream returns `429`, treat that account as exhausted and try another
   healthy account.

The central implementation challenge is that the client-facing auth shape and
the upstream auth shape are different:

- Client to router may need an API-key-shaped placeholder so Claude Code sends
  a request.
- Router to Anthropic must use OAuth Bearer auth plus the OAuth beta header.

## Working Direct Claude OAuth Curl

This is the cheapest known API probe from the research. It sends one Haiku token
and returns rate-limit information in response headers.

```bash
TOKEN="sk-ant-oat01-..."

curl -s -D - -o /dev/null https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

Expected successful behavior:

- HTTP status is in the 2xx range, or a meaningful Anthropic rate-limit response
  is returned.
- Usage/rate-limit information is in headers, not in the JSON body.

Important response headers:

```http
anthropic-ratelimit-unified-status
anthropic-ratelimit-unified-5h-utilization
anthropic-ratelimit-unified-5h-reset
anthropic-ratelimit-unified-7d-utilization
anthropic-ratelimit-unified-7d-reset
anthropic-ratelimit-unified-representative-claim
anthropic-ratelimit-unified-overage-status
```

To show only the rate-limit headers:

```bash
curl -s -D - -o /dev/null https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
  2>/dev/null | grep -i "anthropic-ratelimit-unified"
```

## Working Keychain-Based Usage Probe

The research script `claude_usage.sh` reads Claude Code credentials from macOS
Keychain, refreshes if needed, calls `/v1/messages`, and prints rate-limit
headers.

Credential source:

```bash
security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w
```

Expected JSON shape:

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1774772239299,
    "subscriptionType": "pro",
    "rateLimitTier": "default_claude_ai"
  }
}
```

Some machines may also have additional Claude Code keychain items, such as:

```text
Claude Code-credentials-2
```

Each credential entry can represent a different Claude account and should be
treated as a separate routable token/account.

## Refresh Token Flow

Claude Code OAuth access tokens expire. The router must use the refresh token to
obtain a new access token before sending upstream traffic with an expired token,
or after a `401` that looks like token expiry.

Refresh endpoint:

```text
https://platform.claude.com/v1/oauth/token
```

This is not the same host as the messages API. Do not refresh against
`https://api.anthropic.com`.

Refresh content type:

```http
content-type: application/x-www-form-urlencoded
```

Do not send the refresh body as JSON.

Required refresh fields:

```text
grant_type=refresh_token
refresh_token=<refresh_token>
client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e
scope=user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload
```

Working refresh curl:

```bash
REFRESH_TOKEN="sk-ant-ort01-..."

curl -s https://platform.claude.com/v1/oauth/token \
  -H "content-type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "refresh_token=$REFRESH_TOKEN" \
  --data-urlencode "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" \
  --data-urlencode "scope=user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload"
```

Successful refresh response shape:

```json
{
  "access_token": "sk-ant-oat01-...",
  "refresh_token": "sk-ant-ort01-...",
  "expires_in": 3600
}
```

Important refresh behavior:

- The refresh response may rotate the refresh token.
- If a new `refresh_token` is returned, store it and use it for future refreshes.
- Compute the new `expires_at` from `expires_in`.
- Mark the token healthy after a successful refresh.
- If refresh fails, mark the token unhealthy and try the next account.

For full compatibility with Claude Code itself, the refreshed values should also
be written back to the source keychain JSON. Otherwise, the router may continue
working from its own database while Claude Code's local login state drifts stale.

The keychain update pattern from the research is:

```bash
security delete-generic-password -s "Claude Code-credentials" -a "$(whoami)" 2>/dev/null
security add-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w "$UPDATED_JSON"
```

For router-only operation, updating the router's token database is the minimum
requirement. For keeping Claude Code in sync, update Keychain too.

## Refresh Scopes Matter

The research uses this scope string:

```text
user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload
```

The `user:inference:claude_code` scope is notable. If a refreshed token is
accepted by the token endpoint but rejected by the messages API, compare its
scopes against the research scope string.

Potential symptom of bad or insufficient scope:

```text
403 Permission denied
```

or repeated inference failures after a successful refresh.

## End-To-End Router Test Curl

After the router is running locally:

```bash
python3 -m oauthrouter.cli serve --port 8100 -v
```

test the Anthropic-compatible path with a placeholder client key:

```bash
curl -s -D - http://127.0.0.1:8100/claude/v1/messages \
  -H "x-api-key: oauthrouter" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

For the router to work, its upstream request to Anthropic should differ from
that local curl in these ways:

- Incoming `x-api-key: oauthrouter` is removed.
- Outgoing `Authorization: Bearer <selected_access_token>` is added.
- Outgoing `anthropic-beta: oauth-2025-04-20` is added if the client did not
  provide it.

This is exactly the kind of comparison the logs portal should expose: full
incoming request, full outgoing upstream request, upstream response, and final
client response.

## End-To-End Claude Code Test

Claude Code can be pointed at the router with:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8100/claude \
ANTHROPIC_API_KEY=oauthrouter \
claude --bare -p "hello"
```

Expected flow:

1. Claude Code accepts the placeholder `ANTHROPIC_API_KEY` locally.
2. Claude Code sends a request to `http://127.0.0.1:8100/claude/v1/messages`.
3. The router strips the placeholder auth.
4. The router injects one selected Claude OAuth token as Bearer auth.
5. The router adds the OAuth beta header.
6. Anthropic accepts or rate-limits that selected account.
7. On `429`, the router retries with another healthy account.

If Claude Code prints:

```text
Invalid API key - Fix external API key
```

that usually means Anthropic saw the wrong upstream auth shape, or the router
returned an Anthropic auth failure to Claude Code. Check the logs portal detail
view for:

- whether upstream used `x-api-key` instead of `Authorization: Bearer`
- whether `anthropic-beta` was missing
- whether the selected token was expired
- whether refresh succeeded
- whether the refreshed token had the right scopes

## Common Failure Signatures

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| `Not logged in - Please run /login` before router logs show traffic | Claude Code refused to send traffic without local auth | Use a placeholder `ANTHROPIC_API_KEY` or a client mode that sends requests |
| `401 OAuth authentication is currently not supported` | Bearer OAuth token sent without `anthropic-beta: oauth-2025-04-20` | Add or append the OAuth beta header upstream |
| `Invalid API key - Fix external API key` | Upstream received API-key-shaped auth, or auth failure was surfaced to Claude Code | Strip placeholder keys and inject Bearer OAuth |
| `400 Invalid request format` during refresh | Refresh body/host/client_id is wrong | Use form-urlencoded body, `platform.claude.com`, and the Claude Code client id |
| `403 Permission denied` | Token lacks required inference/model scope | Check scopes, especially `user:inference:claude_code` |
| `429` / "hit your limit" | Selected Claude account is exhausted | Mark that account unhealthy/exhausted and fail over |

## Notes On The Existing Research Scripts

`claude_usage.sh` contains the most relevant working Claude-specific flow:

- reads the Claude Code keychain item
- refreshes expired tokens
- uses the fixed Claude Code client id
- sends Bearer auth
- includes `anthropic-beta: oauth-2025-04-20`
- reads rate-limit state from response headers

`oauth_test.sh` and `claude_oauth_test.sh` are useful generic OAuth examples for
the try-refresh-retry pattern. However, for Claude Pro/Max OAuth inference, make
sure the Claude request includes:

```http
anthropic-beta: oauth-2025-04-20
```

Without that header, a generic Bearer-token OAuth probe can fail even when the
token and refresh flow are otherwise correct.

## Practical Implementation Checklist

- Claude provider upstream: `https://api.anthropic.com`
- Claude refresh endpoint: `https://platform.claude.com/v1/oauth/token`
- Claude upstream auth: `Authorization: Bearer <access_token>`
- Required upstream API header: `anthropic-version: 2023-06-01`
- Required upstream OAuth header: `anthropic-beta: oauth-2025-04-20`
- Refresh content type: `application/x-www-form-urlencoded`
- Refresh client id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- Refresh scope: `user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload`
- Store rotated `refresh_token` values
- Treat `429` as account exhaustion and fail over
- Capture full incoming request, upstream request, upstream response, and final
  response for debugging
