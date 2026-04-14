# OAuthModelRouter Design Spec

## Context

When using multiple Claude and OpenAI subscriptions (each backed by browser OAuth tokens), there is no unified way to manage credentials and route requests across them. Today, you must manually swap tokens, and if one expires mid-session, your tool breaks until you re-extract and reconfigure. This project builds a local proxy that sits between AI coding tools and the upstream APIs, managing multiple OAuth credentials with automatic failover and token refresh.

## Requirements

- **Local reverse proxy** that AI coding tools (Claude Code, Cursor, etc.) connect to
- **Pass-through forwarding** — requests are forwarded as-is to the correct upstream, no API format translation
- **Path-based routing** — `/claude/*` routes to Anthropic, `/openai/*` routes to OpenAI
- **Multiple OAuth tokens per provider** stored in SQLite
- **Failover** — on auth failure, attempt token refresh, then try the next healthy token
- **Automated refresh** — use OAuth refresh_token flow when access tokens expire
- **CLI** for token management (add, remove, list, refresh, status)
- **Streaming support** — SSE streaming pass-through for AI model responses
- **Python** implementation using FastAPI + httpx

## Architecture

```
Caller (Claude Code, Cursor)
    │
    ▼ HTTP
┌──────────────────────────────────┐
│       OAuthModelRouter           │
│  localhost:8000                   │
│                                  │
│  /claude/* ──▶ Token Manager     │
│  /openai/* ──▶ Token Manager     │
│                    │             │
│              Token Store         │
│              (SQLite)            │
└──────────┬───────────┬───────────┘
           │           │
           ▼           ▼
    api.anthropic.com  api.openai.com
```

### Caller Configuration

- Claude Code: `ANTHROPIC_BASE_URL=http://localhost:8000/claude`
- OpenAI tools: `OPENAI_BASE_URL=http://localhost:8000/openai`

## Token Management

### Storage Schema (SQLite)

```sql
CREATE TABLE tokens (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_endpoint TEXT,
    expires_at DATETIME,
    status TEXT DEFAULT 'healthy',
    last_used_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Token Selection (Failover with LRU)

1. Query healthy tokens for the requested provider, ordered by `last_used_at ASC`
2. If the selected token's `expires_at` is past, attempt refresh first
3. If refresh fails, mark unhealthy, move to next token
4. If all tokens unhealthy, return 503 with details

### Token Refresh Flow

1. On 401/403 or expired `expires_at`, POST to `token_endpoint` with `grant_type=refresh_token`
2. On success: update `access_token`, `refresh_token`, `expires_at` in store
3. On failure: mark token `unhealthy`, log warning, try next token
4. Use an `asyncio.Lock` per token to prevent concurrent refresh attempts — if a refresh is already in-flight for a token, other requests wait for it to complete rather than triggering duplicate refreshes

## CLI Interface

```bash
oauthrouter token add --name <name> --provider <claude|openai> \
  --access-token <token> --refresh-token <token> --token-endpoint <url>
oauthrouter token list
oauthrouter token remove <name>
oauthrouter token refresh <name>
oauthrouter serve --port 8000
oauthrouter status
```

## Configuration

File: `~/.oauthrouter/config.toml`

```toml
[server]
host = "127.0.0.1"
port = 8000

[providers.claude]
upstream = "https://api.anthropic.com"
auth_header = "x-api-key"

[providers.openai]
upstream = "https://api.openai.com"
auth_header = "Authorization"
auth_prefix = "Bearer"
```

## Project Structure

```
OAuthModelRouter/
├── pyproject.toml
├── src/
│   └── oauthrouter/
│       ├── __init__.py
│       ├── cli.py              # CLI entry point (typer)
│       ├── server.py           # FastAPI app with proxy routes
│       ├── proxy.py            # Forward request, inject auth
│       ├── token_manager.py    # Token selection, refresh, health
│       ├── token_store.py      # SQLite CRUD
│       ├── config.py           # Load config.toml
│       └── models.py           # Pydantic models
├── tests/
│   ├── test_proxy.py
│   ├── test_token_manager.py
│   └── test_token_store.py
└── README.md
```

### Dependencies

- `fastapi` + `uvicorn` — HTTP server
- `httpx` — async HTTP client (streaming support)
- `typer` — CLI
- `pydantic` — models and validation
- `aiosqlite` — async SQLite
- `tomli` — TOML config parsing

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Token expired, refresh succeeds | Transparent retry |
| Token expired, refresh fails | Mark unhealthy, try next |
| All tokens unhealthy | 503 with dead token list |
| Unknown provider path | 404 |
| Upstream timeout | 504, token stays healthy |
| Upstream 429 | Forward 429, token stays healthy |
| Upstream 5xx | Forward to caller, token stays healthy |

## Observability

- Structured logging with token name (never token value), provider, status code
- `GET /health` endpoint returning provider health summary
- Token health displayed via `oauthrouter status` CLI command

## Verification Plan

1. **Unit tests**: Token store CRUD, token selection logic, refresh flow (mocked HTTP)
2. **Integration test**: Start router, send request with mock upstream, verify forwarding and auth injection
3. **Manual E2E test**:
   - Start router with `oauthrouter serve`
   - Add a real token with `oauthrouter token add`
   - Point Claude Code at `http://localhost:8000/claude`
   - Send a request and verify it reaches the upstream
   - Invalidate a token and verify failover to the next one
