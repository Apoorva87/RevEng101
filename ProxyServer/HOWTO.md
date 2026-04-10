# HOWTO: Running ProxyServer

## Prerequisites

- **Node.js 18+**
- npm (comes with Node.js)

## Install

```bash
npm install
```

## Start the Server

```bash
node server.js
```

Output:

```
=== HTTPS MITM Setup ===
CA certificate: /path/to/ProxyServer/certs/ca.crt

=== ProxyServer Ready ===
  Proxy:     http://localhost:9080
  Dashboard: http://localhost:9081
```

- **Proxy** at `http://localhost:9080` — point your HTTP client here
- **Dashboard** at `http://localhost:9081` — open in browser to view traffic

## Custom Ports

```bash
PROXY_PORT=7070 DASHBOARD_PORT=7071 node server.js
```

## Send Traffic Through the Proxy

### curl

```bash
# HTTP GET
curl -x http://localhost:9080 http://httpbin.org/get

# POST with JSON
curl -x http://localhost:9080 \
  -X POST http://httpbin.org/post \
  -H "Content-Type: application/json" \
  -d '{"user": "admin", "action": "login"}'

# HTTPS (requires CA trust — see below)
curl -x http://localhost:9080 https://httpbin.org/get
```

### macOS System Proxy (Wi-Fi)

```bash
# Enable
networksetup -setwebproxy Wi-Fi localhost 9080
networksetup -setsecurewebproxy Wi-Fi localhost 9080

# Disable when done
networksetup -setwebproxystate Wi-Fi off
networksetup -setsecurewebproxystate Wi-Fi off
```

### Firefox

Settings > General > Network Settings > Manual proxy configuration:
- HTTP Proxy: `localhost`, Port: `9080`
- Check "Also use this proxy for HTTPS"

### Chrome

Chrome uses system proxy on macOS. On Linux:

```bash
google-chrome --proxy-server="http://localhost:9080"
```

## HTTPS Setup

On first run a CA certificate is generated at `certs/ca.crt`. Trust it once:

```bash
# macOS (covers Chrome + Safari)
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain certs/ca.crt

# Firefox — import separately
# Preferences → Privacy & Security → Certificates → View Certificates → Import
```

To remove trust later:

```bash
sudo security remove-trusted-cert -d certs/ca.crt
```

## Interactive Test

Run the included test script to send 5 sample requests one at a time while browsing the dashboard:

```bash
bash test.sh
```

The script opens the dashboard in your browser, then walks you through 5 requests — press Enter to send each one.

## Keyboard Shortcuts (Dashboard)

| Key   | Action                    |
|-------|---------------------------|
| `j`   | Select next request       |
| `k`   | Select previous request   |
| `f`   | Focus URL filter          |
| `i`   | Toggle intercept on/off   |
| `Esc` | Close any open modal      |

## Saving & Exporting

- **Save** — saves current traffic to `sessions/` as JSON
- **Load** — restores a saved session
- **Export HAR** — downloads a HAR 1.2 file for Chrome DevTools import

## AI Chat Agent

The dashboard has an embedded AI chat panel (bottom-right). Requires the `claude` CLI in your PATH.

- Toggle context checkboxes to control what the AI sees
- Type questions about traffic, rules, or source code
- Commands: `/reset`, `/compact`, `/help`

## Stopping the Server

Press `Ctrl+C`. The server shuts down gracefully.
