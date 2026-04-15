# OAuthModelRouter

Local reverse proxy for routing AI coding tools through multiple Claude and OpenAI accounts on one machine.

The router listens on localhost, chooses a token for the requested provider, injects the real upstream auth, and forwards the request. It also exposes a small web portal for token management, provider tests, and request tracing.

## What This Project Is For

Use this when you want tools such as:

- Claude Code
- Codex CLI
- Cursor
- other OpenAI- or Anthropic-compatible clients

to talk to one local endpoint while the router decides which account/token to use.

## Current Model

This repo currently works best with:

- Claude: long-lived Claude tokens stored directly in the router DB
- OpenAI/Codex: tokens stored directly in the router DB

Important nuance:

- Claude request routing goes to `https://api.anthropic.com`
- OpenAI request routing goes to `https://api.openai.com`
- OpenAI health checks do not use the chat completions API; they use ChatGPT usage data from `https://chatgpt.com/backend-api/wham/usage`

## Why Claude Uses Long-Lived Tokens Here

Claude OAuth technically works, but it was too brittle for reliable multi-account local routing. The problems were:

- refresh tokens are single-use
- refresh requires per-login client metadata
- the refresh endpoint can rate-limit by IP
- one bad refresh flow can effectively lock out every Claude account on the machine

Because of that, the practical setup for this router is:

- keep Claude credentials in the router as long-lived tokens
- use the router to select and inject those tokens upstream

The older OAuth notes still live in [CLAUDE.md](docs/research/CLAUDE.md), but for day-to-day usage you should think of Claude here as "multiple long-lived Claude tokens behind one local endpoint".

## How Routing Works

Provider isolation is strict:

- `/claude/*` only uses Claude tokens
- `/openai/*` only uses OpenAI tokens

Examples:

- `http://127.0.0.1:8100/claude/v1/messages` -> `https://api.anthropic.com/v1/messages`
- `http://127.0.0.1:8100/openai/v1/chat/completions` -> `https://api.openai.com/v1/chat/completions`

Selection behavior:

- only `healthy` tokens are eligible by default
- lower `priority` values are selected first
- tokens in a temporary rate-limit cooldown are skipped
- `401/403` can trigger refresh or failover if refresh material exists
- `429` puts the token into cooldown and the router tries another healthy token

This is priority-based routing, not LRU routing.

## Files The Router Uses

The router creates and uses:

- config: `~/.oauthrouter/config.toml`
- database: `~/.oauthrouter/tokens.db`

Default provider config is defined in [src/oauthrouter/config.py](src/oauthrouter/config.py).

## Install

From this repo:

```bash
pip install -e .
```

For development and tests:

```bash
pip install -e ".[dev]"
```

## Start The Router

The simplest way is:

```bash
./scripts/dev/run.sh
```

What `scripts/dev/run.sh` does:

- kills anything already listening on port `8100`
- starts a fresh router from this checkout
- writes process logs to `/tmp/oauthrouter-8100.log`

Useful environment overrides:

```bash
PORT=8200 ./scripts/dev/run.sh
LOG_FILE=/tmp/my-router.log ./scripts/dev/run.sh
```

You can also run it directly:

```bash
PYTHONPATH=src python3 -m oauthrouter.cli serve --port 8100
```

Portal:

```bash
open http://127.0.0.1:8100/portal
```

## Logs And Traces

There are two different logging surfaces.

### Process Logs

If you start with `./scripts/dev/run.sh`, stdout/stderr goes to:

```bash
/tmp/oauthrouter-8100.log
```

Watch it live with:

```bash
tail -f /tmp/oauthrouter-8100.log
```

These logs show things like:

- router startup
- selected token
- upstream URL
- failover after `429`
- auth failure handling
- final response status

### Request Trace API

The portal also exposes in-memory request traces:

- summary list: `GET /api/logs`
- full detail: `GET /api/logs/{id}`

Examples:

```bash
curl -s http://127.0.0.1:8100/api/logs | jq
curl -s http://127.0.0.1:8100/api/logs/<log_id> | jq
```

What this includes:

- incoming request path
- upstream URL
- selected token
- each retry/failover attempt
- captured request/response headers
- bodies when available

Important:

- this trace data is in memory only
- it is capped to the latest 200 entries
- it may contain live auth headers

## Setting Up Tokens

There are three practical ways to populate the DB:

1. Use the web portal
2. Use the CLI manually
3. Use the DB helper script directly

### Recommended Claude Setup

Use long-lived Claude tokens.

In the portal:

- open `/portal`
- add a token manually
- provider: `claude`
- access token: your long-lived Claude token
- set priority so the lower number is preferred first

CLI equivalent:

```bash
oauthrouter token add \
  --name claude-personal \
  --provider claude \
  --access-token sk-ant-... \
  --priority 1
```

Expected behavior:

- the token usually has no expiry in the DB
- there is no refresh flow
- if the token is revoked or exhausted, replace it with a new long-lived token

### Recommended OpenAI / Codex Setup

Use OpenAI/Codex credentials stored in the local DB.

The only supported runtime source is:

```bash
~/.oauthrouter/tokens.db
```

If you already have credentials in `~/.codex/auth.json`, extract the values you need and add them to the DB manually.

The router typically needs:

- `access_token`
- `refresh_token`
- `account_id` if available

The router can also derive the ChatGPT account header from the JWT claims if `account_id` is missing, so the DB entry can stay minimal when you already have a valid JWT.

CLI example:

```bash
oauthrouter token add \
  --name codex-plus \
  --provider openai \
  --access-token eyJ... \
  --refresh-token rt_... \
  --priority 1
```

### Manual DB / Local Helpers

Repo helpers:

- [scripts/dev/run.sh](scripts/dev/run.sh): restart the router
- [scripts/ops/db_tokens.sh](scripts/ops/db_tokens.sh): inspect or edit token rows
- [index.html](index.html): local docs portal for Markdown and HTML notes

Example:

```bash
./scripts/ops/db_tokens.sh list
```

## Pointing Clients At The Router

### Claude Code

Use Claude Code in `--bare` mode with a placeholder key:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8100/claude \
ANTHROPIC_API_KEY=oauthrouter \
claude --bare
```

Why the placeholder key exists:

- Claude Code still wants a non-empty local API key when using a custom base URL
- the router strips that placeholder auth before forwarding
- the router injects the selected Claude token upstream

Persist it in your shell if you want:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8100/claude
export ANTHROPIC_API_KEY=oauthrouter
```

### Codex CLI

Point Codex at the router:

```bash
OPENAI_BASE_URL=http://127.0.0.1:8100/openai codex
```

Persist it:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8100/openai
```

### Other Clients

Set the provider-specific base URL:

- Claude-compatible clients: `http://127.0.0.1:8100/claude`
- OpenAI-compatible clients: `http://127.0.0.1:8100/openai`

## The Web Portal

Portal URL:

```text
http://127.0.0.1:8100/portal
```

Main things it does:

- view tokens and status
- add or edit tokens
- change token priority
- enable or disable tokens
- run provider tests
- inspect request logs and traces

## What Provider Tests Actually Do

This matters because the provider tests are not fake.

### Claude Provider Test

The Claude test sends a real minimal upstream request:

- endpoint: `POST https://api.anthropic.com/v1/messages`
- auth: selected Claude token
- body: tiny prompt asking for `OK`

On success, expect:

- HTTP `200`
- snippet like `OK`
- rate-limit windows populated from Anthropic response headers

### OpenAI Provider Test

The OpenAI test checks the real ChatGPT usage endpoint:

- endpoint: `GET https://chatgpt.com/backend-api/wham/usage`
- auth: selected token's `Authorization: Bearer ...`
- account header: `ChatGPT-Account-Id`

On success, expect:

- HTTP `200`
- snippet like `Plan plus · 5h 18% · 7d 18%`
- OpenAI usage windows shown in the token pane

Important:

- this health check is for token/account usability
- it is not the same as a routed `chat/completions` request
- normal routed OpenAI traffic still goes to `api.openai.com`

## Understanding Status And Rate Limits

### Token Status

`healthy` means the router is willing to pick the token.

`unhealthy` means the router will skip it by default.

Typical reasons a token becomes unhealthy:

- auth failure and refresh failed
- it was manually disabled
- it was imported in a bad state

If you explicitly select an unhealthy token in the provider test and the test succeeds, the router can mark it healthy again.

### Rate-Limit Bars

Rate-limit bars are populated from live upstream data.

They do not appear just because a token is healthy.

They appear after:

- a provider test
- a token test
- or a routed request that returned usable rate-limit metadata

Current sources:

- Claude: upstream Anthropic response headers
- OpenAI: ChatGPT `wham/usage` JSON

The OpenAI usage parser is driven by `limit_window_seconds`, so the labels are derived from the live response rather than mocked in the UI.

## What To Expect In Practice

For Claude:

- long-lived tokens are the reliable path
- provider tests should return a small `OK`
- rate-limit bars usually show `5h` and `7d`

For OpenAI:

- a good Codex auth token should pass the usage check
- the provider test should show your ChatGPT plan and usage percentages
- if multiple DB rows hold the same underlying token, they will behave the same

For both:

- lower `priority` wins
- `429` can temporarily cool down a token
- a request may fail over to another token if one is exhausted

## Troubleshooting

### "No healthy tokens available for provider 'openai'"

The router currently has no healthy OpenAI token to choose automatically.

Things to do:

- re-enable a token in the portal
- explicitly select a token in `Test Provider`
- update or replace the corresponding DB token

### OpenAI Test Shows A Plan But Real OpenAI Requests Still Fail

That means the token is valid enough for ChatGPT usage introspection, but your actual routed request may still hit model-specific or quota-specific behavior at `api.openai.com`.

Use:

- process logs
- `/api/logs`
- `/api/logs/{id}`

to inspect the real routed request path.

### Claude Token Is Healthy But No Rate Limit Bars Are Visible

Run a live Claude provider test or send a real request through the router. The bars are only populated after the router sees real upstream rate-limit data.

### Claude OAuth Refresh Still Exists In Config

That is mostly legacy compatibility. The recommended operational model for this repo is long-lived Claude tokens in the DB.

## Development

Run tests:

```bash
python3 -m pytest -q
```

Code layout:

```text
src/oauthrouter/
  cli.py
  config.py
  models.py
  proxy.py
  server.py
  token_manager.py
  token_store.py
  static/portal.html
```

## Summary

If you only need the practical path:

1. Add long-lived Claude tokens to the router.
2. Add OpenAI/Codex tokens to `~/.oauthrouter/tokens.db`.
3. Start the server with `./scripts/dev/run.sh`.
4. Point Claude Code at `/claude` and Codex at `/openai`.
5. Use the portal to test providers and inspect logs.
