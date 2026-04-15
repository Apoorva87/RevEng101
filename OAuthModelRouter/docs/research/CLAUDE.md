# OAuthModelRouter

Local reverse proxy that manages multiple API tokens for Claude and OpenAI APIs.
AI coding tools (Claude Code, Codex CLI, Cursor, etc.) connect to `localhost:8000` and the
router handles credential injection, token selection, and failover across multiple subscription accounts.

The project now uses **long-lived API keys** for both Claude accounts instead of short-lived
OAuth tokens. This avoids the fragility of OAuth token refresh (single-use refresh tokens,
IP-based rate limiting on the refresh endpoint, per-session client_id requirements).

## Quick Start

```bash
# Install (from this directory)
pip install -e .

# Start the router (default port 8000)
oauthrouter serve

# Or on a custom port with debug logging
oauthrouter serve --port 8100 -v

# Open the web portal
open http://localhost:8100/portal
```

## Pointing AI Tools at the Router

### Claude Code

```bash
# Override the API base URL to point at the router's /claude/ prefix.
# Claude Code still requires ANTHROPIC_API_KEY to be set when a custom
# Anthropic base URL is used. The value can be any non-empty placeholder:
# the router overwrites it with the selected OAuth token before forwarding.
ANTHROPIC_BASE_URL=http://localhost:8000/claude \
ANTHROPIC_API_KEY=oauthrouter \
claude --bare

# To persist it, add to your shell profile:
export ANTHROPIC_BASE_URL=http://localhost:8000/claude
export ANTHROPIC_API_KEY=oauthrouter
```

Use `--bare` when you want Claude Code to be account-agnostic. It skips Claude
Code's local OAuth/keychain login checks and uses the API-key-style request path,
which lets OAuthModelRouter choose the upstream account.

### Codex CLI (OpenAI)

```bash
# Override the API base URL to point at the router's /openai/ prefix
OPENAI_BASE_URL=http://localhost:8000/openai codex

# To persist it:
export OPENAI_BASE_URL=http://localhost:8000/openai
```

### Cursor / Other Tools

Set the API base URL in the tool's settings to `http://localhost:8000/claude` or
`http://localhost:8000/openai` depending on the provider.

## How Routing Works

Requests to `/{provider}/{path}` are forwarded to the upstream API:

- `localhost:8000/claude/v1/messages` -> `https://api.anthropic.com/v1/messages`
- `localhost:8000/openai/v1/chat/completions` -> `https://api.openai.com/v1/chat/completions`

The router selects a token using LRU (least-recently-used) ordering among healthy tokens for
that provider, injects the auth credentials, and forwards the request. If upstream returns
401/403, it refreshes the token or fails over to the next healthy one.

**Provider isolation**: Claude tokens are ONLY used for `/claude/*` routes. OpenAI tokens are
ONLY used for `/openai/*` routes. Cross-provider routing is not possible.

## Token Sources

The router is DB-only now. Supported operational paths are:

```bash
oauthrouter token add ...
oauthrouter token list
oauthrouter token remove ...
oauthrouter token refresh ...
./scripts/ops/db_tokens.sh list
```

All runtime token state lives in `~/.oauthrouter/tokens.db`.

## Token Management (CLI)

```bash
oauthrouter token list                          # list all tokens
oauthrouter token list --provider claude        # filter by provider
oauthrouter token add -n my-claude -p claude -a sk-ant-...  # add manually
oauthrouter token remove my-claude              # delete a token
oauthrouter token refresh my-claude             # manually trigger refresh
oauthrouter status                              # show router + token health
```

## Token Authentication

### Current Setup: Long-Lived API Keys

Both Claude accounts now use **long-lived API keys** stored directly in the router's
SQLite DB. This is the recommended approach — no refresh flow needed, no expiry concerns.

### Legacy: OAuth Token Refresh (historical reference)

The router previously used short-lived OAuth tokens from the macOS Keychain. This was
fragile due to:
- **Single-use refresh tokens**: each refresh returns a new refresh_token, old one invalidated
- **Per-session client_id**: each Claude Code login creates a unique `oauthClientId` (UUID)
- **IP-based rate limiting**: the refresh endpoint (`platform.claude.com/v1/oauth/token`)
  rate-limits by IP, not by account — failed refresh attempts for one account lock out ALL
  accounts on that machine
- **Claude Code single-session design**: Claude Code does not officially support multiple
  active accounts on one machine (tracked: github.com/anthropics/claude-code/issues/24963).
  Workaround: `CLAUDE_CONFIG_DIR` isolation creates separate keychain entries per profile.

## Architecture

```
src/oauthrouter/
  models.py         # Pydantic models: Token, ProviderConfig, AppConfig
  config.py         # TOML config from ~/.oauthrouter/config.toml
  token_store.py    # Async SQLite CRUD (tokens.db)
  token_manager.py  # LRU selection, refresh, failover logic
  proxy.py          # HTTP forwarding with auth injection + streaming
  server.py         # FastAPI routes (portal, API, proxy catch-all)
  cli.py            # Typer CLI (serve, token, status)
  static/portal.html # Web management portal
```

**Config**: `~/.oauthrouter/config.toml`
**Database**: `~/.oauthrouter/tokens.db` (SQLite)

## Auth Header Conventions

The Anthropic API accepts two auth methods: `x-api-key` for API keys and
`Authorization: Bearer` for OAuth tokens. Since the router manages OAuth tokens
(not API keys), it sends them as Bearer tokens.

**Critical: OAuth requires a beta header.** The Anthropic API will reject OAuth tokens
with "OAuth authentication is currently not supported" unless the request includes:
```
anthropic-beta: oauth-2025-04-20
```

This is configured as `extra_headers` in the Claude provider config and injected
automatically by the proxy. If a client already sends `anthropic-beta` with other
values, the router merges them (comma-separated).

| Provider | Header | Format | Extra Headers |
|----------|--------|--------|---------------|
| Claude (Anthropic) | `Authorization` | `Bearer sk-ant-oat01-...` | `anthropic-beta: oauth-2025-04-20` |
| OpenAI | `Authorization` | `Bearer eyJ...` | (none) |

The router strips any incoming `x-api-key`, `Authorization`, or `api-key` headers
from the client request before injecting the real OAuth credential.

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/
```

## Development Notes

- Python >=3.9 compatible (uses `typing.Dict` not `dict[]` for Pydantic)
- FastAPI route order matters: portal/API routes must be registered BEFORE the catch-all proxy
- Token model stores `oauth_client_id` and `scopes` per-token for Claude refresh
- DB migrations run on startup (ALTER TABLE for new columns, errors ignored if already exist)
- The `_resolve_client_id()` method checks token-level first, then falls back to provider config

## Troubleshooting History

### OAuth beta header required (2026-04-12)

The Anthropic API requires `anthropic-beta: oauth-2025-04-20` header to accept OAuth
Bearer tokens. Configured as `extra_headers` in the Claude provider config, injected
automatically by the proxy and merged with any existing beta values.

### Claude Code multi-account limitation (2026-04-12)

Claude Code is single-account-per-machine by design. Two keychain entries
(`Claude Code-credentials`, `Claude Code-credentials-2`) can exist via `CLAUDE_CONFIG_DIR`
isolation, but they share the same macOS user. The OAuth refresh endpoint rate-limits
by IP (not account), so failed refresh attempts for one account lock out all accounts
on that machine. This was the primary motivation for switching to long-lived API keys.
