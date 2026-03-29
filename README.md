# RevEng101

Reverse engineering projects exploring API auth flows, OAuth token mechanics, and usage tracking for AI coding tools.

## Projects

### [Usage Dashboard](UsageDashboard/)

Reverse engineering Claude Code and Codex/OpenAI authorization — OAuth flows, token refresh, credential storage, and a unified terminal dashboard for monitoring account usage across providers.

- **[usage_hub.py](UsageDashboard/usage_hub.py)** — Curses-based TUI that tracks Claude and Codex/OpenAI usage in real time
- **Claude** — OAuth token flow analysis, binary reverse engineering, keychain credential extraction
- **Codex** — Auth state discovery, refresh token recovery, ChatGPT usage API reverse engineering

#### Tree View
Expanded view with per-model usage breakdowns, auth details, and refresh timing.

![Usage Dashboard — Tree View](screenshots/dashboard_tree_view.png)

#### Grid View
Side-by-side account comparison with usage bars and reset countdowns.

![Usage Dashboard — Grid View](screenshots/dashboard_grid_view.png)

## Browse

Open [`index.html`](index.html) in a browser or visit the [GitHub Pages site](https://apoorva87.github.io/RevEng101/) to navigate all project docs.

## Setup

```bash
cd UsageDashboard
pip install requests
python usage_hub.py
```

See the [UsageDashboard README](UsageDashboard/README.md) for full usage details.
