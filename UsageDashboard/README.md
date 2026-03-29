# Usage Dashboard

A unified terminal dashboard for monitoring Claude and Codex/OpenAI account usage, built on `curses`.

## Prerequisites

- Python 3.9+
- `requests` library (`pip install requests`)
- macOS (uses Keychain for Claude OAuth credentials)

## Quick Start

```bash
cd UsageDashboard
python usage_hub.py
```

On first launch the dashboard prompts you to add an account (Claude OAuth, Codex OAuth, or OpenAI API key). Accounts are saved to `.local/usage_hub.json` so they persist between sessions.

### Skip the startup prompt

```bash
python usage_hub.py --no-startup-prompt
```

### Use a custom config file

```bash
python usage_hub.py --config /path/to/config.json
```

## Keyboard Controls

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh all accounts |
| `Enter` | Refresh selected account |
| `a` | Add a new account |
| `d` | Delete selected account |
| `x` | Hide / show selected account |
| `v` | Toggle visibility of hidden accounts |
| `m` | Edit models for selected account |
| `+` / `-` | Increase / decrease global refresh interval |
| `]` / `[` | Increase / decrease per-account refresh interval |
| `j` / `k` | Navigate up / down |
| `Left` / `Right` | Fold / unfold account details |
| `g` | Toggle grid view |
| `h` | Show help |

## Account Types

| Type | Auth Method | What it tracks |
|------|------------|----------------|
| **Claude OAuth** | macOS Keychain token | Per-model usage via Anthropic API |
| **Codex OAuth** | `~/.codex/auth.json` | Aggregate ChatGPT/Codex usage |
| **OpenAI API** | API key | Per-model usage via OpenAI API |

## File Layout

```
UsageDashboard/
  usage_hub.py          # Main dashboard script
  .local/               # Local config & credentials (git-ignored)
  Claude/               # Claude auth scripts & docs
  Codex/                # Codex/OpenAI auth scripts & docs
```
