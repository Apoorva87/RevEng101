# Claude Code OAuth & Usage API — Quick Reference for AI Models

> Abridged guide for AI models to quickly replicate Claude Code's usage-checking flow.
> Reverse-engineered from Claude Code v2.1.87 on macOS. Last verified: 2026-03-28.

HTML companions in this folder:
- `claude_oauth_guide.html`
- `binary_analysis_guide.html`

## TL;DR

Claude Pro/Max users authenticate via OAuth, not API keys. Usage stats come from **response headers** on normal API calls — there is no dedicated usage endpoint. The key requirement is a beta header.

---

## 1. Read Credentials (macOS)

Tokens are in the macOS Keychain, not on disk:

```bash
CREDS_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w)
```

The JSON structure:
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

Extract the token:
```bash
TOKEN=$(echo "$CREDS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['accessToken'])")
```

## 2. Check Usage (API Call)

**Endpoint:** `POST https://api.anthropic.com/v1/messages`

**Required headers:**
```
Authorization: Bearer <access_token>
anthropic-version: 2023-06-01
anthropic-beta: oauth-2025-04-20        ← REQUIRED for OAuth tokens
content-type: application/json
```

**Minimal request body** (cheapest possible — 1 Haiku token):
```json
{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}
```

**Usage data is in response HEADERS (not body):**
```
anthropic-ratelimit-unified-status: allowed|limited
anthropic-ratelimit-unified-5h-utilization: 0.0-1.0
anthropic-ratelimit-unified-5h-reset: <unix_epoch>
anthropic-ratelimit-unified-7d-utilization: 0.0-1.0
anthropic-ratelimit-unified-7d-reset: <unix_epoch>
anthropic-ratelimit-unified-representative-claim: five_hour|seven_day
anthropic-ratelimit-unified-overage-status: allowed|rejected
```

**Complete curl:**
```bash
curl -s -D - -o /dev/null https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
  2>/dev/null | grep "anthropic-ratelimit-unified"
```

## 3. Refresh Expired Tokens

**Endpoint:** `POST https://platform.claude.com/v1/oauth/token`
*(Note: different host from the API!)*

**Content-Type:** `application/x-www-form-urlencoded` (NOT JSON)

**Parameters:**
| Param | Value |
|-------|-------|
| `grant_type` | `refresh_token` |
| `refresh_token` | From keychain JSON |
| `client_id` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| `scope` | `user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload` |

**curl:**
```bash
curl -s https://platform.claude.com/v1/oauth/token \
  -H "content-type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token&refresh_token=$REFRESH_TOKEN&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&scope=user%3Ainference%20user%3Ainference%3Aclaude_code%20user%3Asessions%3Aclaude_code%20user%3Amcp_servers%20user%3Afile_upload"
```

**Response (success):**
```json
{"access_token":"sk-ant-oat01-...","refresh_token":"sk-ant-ort01-...","expires_in":3600}
```

**After refresh:** Update the keychain so Claude Code stays in sync:
```bash
security delete-generic-password -s "Claude Code-credentials" -a "$(whoami)" 2>/dev/null
security add-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w "$UPDATED_JSON"
```

## 4. Common Mistakes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Missing `anthropic-beta` header | `401 "OAuth authentication is currently not supported"` | Add `anthropic-beta: oauth-2025-04-20` |
| JSON body for token refresh | `400 "Invalid request format"` | Use `application/x-www-form-urlencoded` |
| Wrong host for refresh | `400 "Invalid request format"` | Use `platform.claude.com`, not `api.anthropic.com` |
| Missing `client_id` in refresh | `400 "Invalid request format"` | Include `client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| Using `x-api-key` header | Won't work for Pro/Max | Use `Authorization: Bearer` with OAuth token |

## 5. How This Was Discovered

The reverse-engineering method, for reference:

1. **Keychain search:** `security dump-keychain | grep claude` → found credential storage
2. **Binary string extraction:** `strings /path/to/claude | grep <pattern>` → found:
   - Beta header: `files-api-2025-04-14,oauth-2025-04-20`
   - Client ID: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
   - Token URL: `https://platform.claude.com/v1/oauth/token`
   - Scopes: `eK_=[...,"user:sessions:claude_code","user:mcp_servers","user:file_upload"]`
   - Request shape: `{grant_type:"refresh_token",refresh_token:H,client_id:m8().CLIENT_ID,...}`
3. **Binary location:** `readlink -f $(which claude)` → `~/.local/share/claude/versions/<version>`

## 6. Rate Limit Model

Pro/Max plans use **two rolling windows**:

- **5-hour window** — short-term burst limit, resets on a rolling basis
- **7-day window** — long-term sustained limit

The `representative-claim` header tells you which window is the current bottleneck. When a window is exhausted:
- **Pro:** `overage-status: rejected` — requests are blocked until reset
- **Max:** `overage-status: allowed` — can exceed with potential throttling
