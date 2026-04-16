# Usage Dashboard

This repo currently ships four local dashboards/tools.
`unified_dashboard.py` is the recommended primary session view; the Claude- and Codex-specific dashboards remain as narrower deep-dive tools.

- `usage_hub_web.py`: browser-based dashboard for Claude and Codex/OpenAI account usage
- `claude_sessions_dashboard.py`: local Claude Code session/project analytics from `~/.claude`
- `codex_sessions_dashboard.py`: local Codex session history and token analytics
- `unified_dashboard.py`: combined Claude + Codex session dashboard

### Account Dashboard
Interactive account cards with live usage windows, reset timing, OAuth discovery, and account management.

![Usage Dashboard — Tree View](../screenshots/dashboard_tree_view.png)

### Grid View
Side-by-side account comparison with usage bars and reset countdowns.

![Usage Dashboard — Grid View](../screenshots/dashboard_grid_view.png)

## Prerequisites

- Python 3.9+
- `requests` (`pip install requests`)
- macOS for Claude OAuth keychain discovery/refresh flows

## Usage Hub Web

```bash
cd UsageDashboard
python usage_hub_web.py
```

By default it starts a local server at `http://127.0.0.1:8765/` and opens it in your browser.

### Options

```bash
python usage_hub_web.py --host 127.0.0.1 --port 8765 --no-browser
```

```bash
python usage_hub_web.py --config /path/to/config.json
```

### Account Types

| Type | Auth Method | What it tracks |
|------|------------|----------------|
| **Claude OAuth** | macOS Keychain token | Per-model usage via Anthropic API |
| **Codex OAuth** | `~/.codex/auth.json` | Aggregate ChatGPT/Codex usage |
| **OpenAI API** | API key | Per-model usage via OpenAI API |

## Claude Sessions Dashboard

```bash
python3 claude_sessions_dashboard.py
```

This dashboard shows:

- project and session views sourced from local Claude Code `.jsonl` session files
- inferred session state such as waiting, error, or rate-limited
- token totals extracted from per-message usage fields
- sliding windows such as 5-day and 7-day token totals
- day/hour activity breakdowns with project/session attribution

See `Claude/docs/claude_local_data_dashboard.md` for the data layout and extraction notes.

## Codex Sessions Dashboard

```bash
python codex_sessions_dashboard.py
```

By default it starts a local server at `http://127.0.0.1:8877/` and reads local session logs from `~/.codex/sessions`.

### Options

```bash
python codex_sessions_dashboard.py --host 127.0.0.1 --port 8877 --sessions-dir ~/.codex/sessions --no-browser
```

## Unified Sessions Dashboard

```bash
python unified_dashboard.py
```

By default it starts a local server at `http://127.0.0.1:8878/` and aggregates the Claude and Codex session providers into one view.

## File Layout

```text
UsageDashboard/
  usage_hub_web.py            # Local browser dashboard for account usage
  usage_hub_core.py           # Shared account/auth/refresh logic
  claude_sessions_dashboard.py
  codex_sessions_dashboard.py
  unified_dashboard.py
  .local/                     # Local config & credentials (git-ignored)
  Claude/                     # Claude auth scripts & docs
  Codex/                      # Codex/OpenAI auth scripts & docs
```
