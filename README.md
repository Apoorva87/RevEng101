# RevEng101

Reverse engineering projects exploring API auth flows, OAuth token mechanics, and usage tracking for AI coding tools.

## Projects

### [Usage Dashboard](UsageDashboard/)

Reverse engineering Claude Code and Codex/OpenAI authorization — OAuth flows, token refresh, credential storage, and a unified terminal dashboard for monitoring account usage across providers.

- **[usage_hub.py](UsageDashboard/usage_hub.py)** — Curses-based TUI that tracks Claude and Codex/OpenAI usage in real time
- **Claude** — OAuth token flow analysis, binary reverse engineering, keychain credential extraction
- **Codex** — Auth state discovery, refresh token recovery, ChatGPT usage API reverse engineering

### [OAuthModelRouter](OAuthModelRouter/)

Local reverse proxy for Claude and OpenAI-compatible tools that routes requests through multiple accounts on one machine.

- token discovery from local auth material
- request tracing and provider health checks
- docs on Claude/OpenAI OAuth flows and local routing behavior

### [ProxyServer](ProxyServer/)

Local HTTP/HTTPS forward proxy with a real-time dashboard for reverse engineering and traffic analysis.

- inspect paired requests and responses live
- intercept and modify requests before they leave your machine
- TLS certificate generation and HTTPS MITM docs under [`ProxyServer/docs/index.html`](ProxyServer/docs/index.html)

## Browse

Open [`index.html`](index.html) in a browser or visit the [GitHub Pages site](https://apoorva87.github.io/RevEng101/) to browse all project docs from one place.

## Setup

```bash
cd UsageDashboard
pip install requests
python usage_hub.py
```

See the [UsageDashboard README](UsageDashboard/README.md) for full usage details.
