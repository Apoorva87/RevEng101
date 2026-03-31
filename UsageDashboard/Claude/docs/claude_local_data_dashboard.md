# Claude Local Data Dashboard Notes

This dashboard reads local Claude Code data from `~/.claude` and does not call any remote APIs.

## Where the data lives

- `~/.claude/projects/<project-slug>/<session-id>.jsonl`
  - Primary source of truth for per-session data.
  - Each file is a JSONL event stream for one Claude Code session.
  - Includes user prompts, assistant replies, tool use/tool results, timestamps, cwd/project metadata, and per-message `usage`.
- `~/.claude/history.jsonl`
  - Lightweight prompt history.
  - Helpful for last access timestamps and prompt previews.
- `~/.claude/stats-cache.json`
  - Claude Code's cached aggregate stats.
  - Contains daily activity, daily model tokens, total sessions/messages, and model-level token summaries.
- `~/.claude/settings.json`
  - Local Claude Code settings.
- `~/.claude/telemetry/*.json`
  - Internal event logs. Useful for reverse engineering startup/runtime behavior, but not required for the dashboard's token/session totals.
- `~/.claude/file-history/<session-id>/...`
  - File backup history keyed by session. Useful for edit/version inspection, but not needed for the session analytics views.

## How token usage is extracted

Token usage is taken from assistant message events inside each session `.jsonl` file:

- `message.usage.input_tokens`
- `message.usage.output_tokens`
- `message.usage.cache_read_input_tokens`
- `message.usage.cache_creation_input_tokens`
- `message.usage.server_tool_use.web_search_requests`

The dashboard computes:

- `total_tokens = input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens`
- per-session totals
- per-model totals inside a session
- per-day and per-hour rollups by session/project
- sliding-window totals such as 5-day and 7-day totals

## How session state is inferred

Session state is inferred heuristically from the tail of each session event stream:

- `rate_limited`
  - A recent event has `error == "rate_limit"`.
- `error`
  - A recent event has another error flag, or a tool result error was recorded.
- `awaiting_tool_result`
  - The latest meaningful assistant event ended with `stop_reason == "tool_use"`.
- `awaiting_assistant`
  - The latest meaningful event is a user prompt.
- `waiting_for_user`
  - The latest meaningful event is an assistant reply or a completed turn marker.
- `unknown`
  - Fallback when the file does not contain enough structured events.

These are local inference states, not official Claude states.

## Views the dashboard supports

- Sessions view
  - last access
  - prompt preview
  - total tokens and selected-range tokens
  - inferred state
  - per-model usage totals
- Projects view
  - session counts
  - project token totals
  - range token totals
  - state breakdown across sessions
- Activity view
  - day or hour buckets
  - token totals
  - prompt counts
  - assistant message counts
  - breakdown by project/session for each bucket

## Run

```bash
python3 claude_sessions_dashboard.py --no-browser
```

Then open the printed local URL in a browser.
