# ProxyServer

A local HTTP/HTTPS forward proxy with a real-time web dashboard for reverse engineering, API debugging, and traffic analysis. Route your traffic through it, see every request and response paired in a dark-themed UI, and optionally intercept and modify requests before they leave your machine.

> **Full documentation**: Open [`docs/index.html`](docs/index.html) in your browser for comprehensive, browsable HTML docs covering setup, TLS certificates, the dashboard, interception, AI chat, architecture deep-dives, and the full API reference.

```
                                   you are here
                                       |
Browser/App ──► Proxy (:9080) ──► [inspect] ──► Upstream Server
                     |
                     ▼
              Traffic Store (ring buffer, 5000 entries)
                     |
                     ▼ WebSocket
              Dashboard UI (:9081)
```

## Quick Start

```bash
npm install
node server.js
```

Then configure your HTTP client to proxy through `localhost:9080` and open `http://localhost:9081` in your browser.

```
=== ProxyServer Ready ===
  Proxy:     http://localhost:9080
  Dashboard: http://localhost:9081

Configure your browser/system HTTP proxy to localhost:9080
Then open the dashboard URL in your browser.
```

## Usage Examples

### 1. Inspect API traffic with curl

```bash
# Send a request through the proxy
curl -x http://localhost:9080 http://httpbin.org/get

# POST with JSON body — visible in the dashboard with syntax highlighting
curl -x http://localhost:9080 \
  -X POST http://httpbin.org/post \
  -H "Content-Type: application/json" \
  -d '{"user": "admin", "action": "login"}'

# The dashboard will show:
#   #1  GET   200  httpbin.org  /get   JSON  245B  120ms
#   #2  POST  200  httpbin.org  /post  JSON  512B   89ms
```

### 2. Configure macOS system proxy

```bash
# Enable for all HTTP traffic on Wi-Fi
networksetup -setwebproxy Wi-Fi localhost 9080

# Enable for HTTPS (requires CA trust — see below)
networksetup -setsecurewebproxy Wi-Fi localhost 9080

# Disable when done
networksetup -setwebproxystate Wi-Fi off
networksetup -setsecurewebproxystate Wi-Fi off
```

### 3. Configure Firefox to use the proxy

Firefox Settings > General > Network Settings > Manual proxy configuration:
- HTTP Proxy: `localhost`, Port: `9080`
- Check "Also use this proxy for HTTPS"

### 4. HTTPS inspection (MITM)

On first run, ProxyServer generates a root CA at `certs/ca.crt`. Trust it once:

```bash
# macOS — add to system keychain
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain certs/ca.crt

# Firefox — must import separately
# Preferences → Privacy & Security → Certificates → View Certificates → Import

# Chrome on macOS uses system keychain, so the above command covers it
```

Now HTTPS traffic is decrypted and fully visible in the dashboard. Per-host certificates are generated on the fly, cached in `certs/hosts/`, and signed by your local CA.

For a deep dive into how TLS certificates work, how MITM interception operates, and how ProxyServer generates and manages certificates, see **[docs/certificates.html](docs/certificates.html)** (or the original markdown version at [docs/certificates.md](docs/certificates.md)).

### 5. Intercept and modify a request

1. Click **Rules** in the top bar
2. Add a rule: URL pattern `*api/login*`, Method `POST`, Direction `request`
3. Click **Intercept: OFF** to toggle it **ON** (or press `i`)
4. Trigger the request from your app/browser
5. The request pauses — an edit modal appears in the dashboard
6. Change the JSON body, headers, or method
7. Click **Forward** to send the modified request, or **Drop** to kill it

Intercepted requests auto-forward after 5 minutes to prevent connection leaks.

### 6. Save and reload sessions

```bash
# From the dashboard UI:
#   Save    → writes to sessions/session-2026-03-31T14-30-00.json
#   Load    → pick a saved session to restore into the traffic list
#   Export HAR → download a HAR 1.2 file importable by Chrome DevTools
```

### 7. Use environment variables

```bash
# Custom ports
PROXY_PORT=7070 DASHBOARD_PORT=7071 node server.js
```

### 8. AI Chat Agent

The dashboard includes an embedded AI chat panel (bottom-right corner) powered by Claude Code. It can see your captured traffic, understand the proxy architecture, and take actions.

**Requirements:** The `claude` CLI must be in your PATH.

**Features:**
- Ask questions about captured traffic ("how many POST requests to /api?")
- Generate curl commands from captured requests
- Create intercept rules using natural language
- Understand the proxy source code (toggle "Source" context ON)
- Receive browser cookies/localStorage via the optional Chrome extension in `extension/`

**Commands:** `/reset` (clear chat), `/compact` (compress context), `/help`

**Context toggles:** Control what the AI sees — Traffic summaries, Selected entry detail, Intercept rules, Source code, Browser context. A token budget bar shows the cost of each context block.

See **[docs/ai-chat.html](docs/ai-chat.html)** for full documentation.

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          server.js (entry point)                     │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────────────┐  │
│  │ TrafficStore  │   │  RuleStore   │   │    CertManager          │  │
│  │ (ring buffer) │   │ (rules.json) │   │ (CA + per-host certs)   │  │
│  └──────┬───────┘   └──────┬───────┘   └───────────┬─────────────┘  │
│         │                  │                        │                │
│         │            ┌─────┴──────┐                 │                │
│         │            │ RuleEngine │                 │                │
│         │            └─────┬──────┘                 │                │
│         │                  │                        │                │
│  ┌──────┴──────────────────┴────────────────────────┴──────────┐    │
│  │                     ProxyServer (:9080)                      │    │
│  │                                                              │    │
│  │  HTTP handler ──► RequestInterceptor ──► upstream            │    │
│  │       │                (hold/forward/drop)                   │    │
│  │       │           ResponseInterceptor ◄── upstream           │    │
│  │       │                (hold/forward/drop responses)         │    │
│  │       │                                                      │    │
│  │  CONNECT handler ──► TLSHandler ──► decrypt ──► upstream     │    │
│  │                       • TLS termination via generated certs  │    │
│  │                       • HTTP/2 ALPN to upstream (h2/h1.1)   │    │
│  │                       • Chunked transfer-encoding parsing   │    │
│  │                       • Keep-alive multi-request per tunnel  │    │
│  └──────────────────────────────────────────────────────────────┘    │
│         │                                                            │
│         │ EventEmitter (add/update/clear)                            │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                   DashboardServer (:9081)                     │    │
│  │                                                               │    │
│  │  Static files ← static/index.html, app.js, app.css           │    │
│  │  REST API     ← /api/traffic, /api/rules, /api/sessions, ... │    │
│  │  WSBridge     ← WebSocket push (add/update/clear events)     │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                              │                                       │
│                              ▼ WebSocket                             │
│                     ┌────────────────┐                               │
│                     │  Browser UI    │                               │
│                     │  (vanilla JS)  │                               │
│                     └────────────────┘                               │
└─────────────────────────────────────────────────────────────────────┘
```

### Request Lifecycle

```
1. Client sends request via proxy
         │
         ▼
2. ProxyServer creates TrafficEntry (state: pending)
         │
         ▼
3. Request body collected into buffer (up to 2MB captured)
         │
         ▼
4. Request intercept check ── no match ──► skip to step 7
         │
         ▼ (match + intercept ON, phase: request)
5. Entry state → "intercepted", request held via Promise
         │
         ▼
6. Dashboard shows edit modal ──► user clicks Forward/Drop
         │                              │
         │  ┌───────────────────────────┘
         │  │
         ▼  ▼
7. Entry state → "forwarded", request sent to upstream
   (HTTPS: HTTP/2 tried first via ALPN, falls back to H1)
         │
         ▼
8. Response received, full body buffered
   (chunked transfer-encoding reassembled)
   (gzip/br/deflate decompressed for display)
         │
         ▼
9. Response intercept check ── no match ──► skip to step 12
         │
         ▼ (match + intercept ON, phase: response)
10. Entry state → "intercepted", response held via Promise
         │
         ▼
11. Dashboard shows edit modal (response fields) ──► Forward/Drop
         │
         ▼
12. Response forwarded to client, entry state → "completed"
         │
         ▼
13. WebSocket broadcasts update → dashboard renders in real time
```

### HTTPS MITM Flow

```
Client                    Proxy                         Upstream
  │                         │                              │
  │── CONNECT host:443 ───►│                              │
  │                         │                              │
  │◄── 200 Established ────│                              │
  │                         │                              │
  │══ TLS handshake ═══════│  (proxy presents generated   │
  │   (client sees cert     │   cert for "host", signed    │
  │    signed by our CA)    │   by certs/ca.crt)           │
  │                         │                              │
  │── GET /path (encrypted)│                              │
  │                         │── TLS connect ──────────────►│
  │                         │── GET /path (re-encrypted) ─►│
  │                         │                              │
  │                         │◄── 200 OK ──────────────────│
  │◄── 200 OK (encrypted) ─│                              │
  │                         │                              │
  │  (proxy sees plaintext  │                              │
  │   request + response)   │                              │
```

### Data Model: TrafficEntry

Each captured request/response pair is a `TrafficEntry` with this structure:

```
TrafficEntry
├── id           (UUID)
├── seq          (monotonic counter — displayed as #1, #2, ...)
├── state        pending → intercepted → forwarded → completed
│                                                  → error
│                                                  → aborted
├── request
│   ├── method, url, httpVersion
│   ├── headers   (object)
│   ├── body      (Buffer, max 2MB captured)
│   └── contentType
├── response
│   ├── statusCode, statusMessage
│   ├── httpVersion   (e.g. "2" for H2, "1.1" for H1)
│   ├── headers
│   ├── body      (Buffer, max 2MB captured, decompressed)
│   └── contentType
├── target
│   ├── host, port, protocol
├── timing
│   ├── start, ttfb, end, duration (ms)
├── intercept
│   ├── wasIntercepted, wasModified, matchedRuleId
│   └── phase          ('request' or 'response' — which is held)
└── clientIp, clientPort
```

Bodies larger than 2MB are still forwarded in full — the cap is display/storage only.

### Intercept Rule Schema

Rules are persisted to `rules.json` and support these match criteria:

| Field          | Description                          | Example            |
|----------------|--------------------------------------|--------------------|
| `urlPattern`   | Glob pattern matched against full URL | `*api/users*`      |
| `method`       | HTTP method (`*` = any)              | `POST`             |
| `contentType`  | Substring match on content-type      | `json`             |
| `headerKey`    | Header name to match                 | `authorization`    |
| `headerValue`  | Substring match on header value      | `Bearer`           |
| `direction`    | `request`, `response`, or `both`     | `request`          |

## Project Structure

```
ProxyServer/
├── server.js                          Entry point — wires everything, starts both servers
├── package.json                       Only 2 dependencies: ws, node-forge
│
├── src/
│   ├── proxy/
│   │   ├── proxy-server.js            HTTP forward proxy (absolute-URI + CONNECT)
│   │   ├── cert-manager.js            CA generation + per-host cert generation + cache
│   │   ├── tls-handler.js             TLS termination + upstream TLS + stream piping
│   │   ├── request-interceptor.js     Promise-based hold/forward/drop for requests
│   │   └── response-interceptor.js    Same for responses
│   │
│   ├── traffic/
│   │   ├── traffic-entry.js           Data model (UUID, state machine, timing, 2MB cap)
│   │   ├── traffic-store.js           Ring buffer (5000 entries) + EventEmitter
│   │   └── har-export.js              Export to HAR 1.2 format
│   │
│   ├── dashboard/
│   │   ├── dashboard-server.js        Static file server + full REST API + chat routes
│   │   └── ws-bridge.js               WebSocket bridge: traffic events + chat routing
│   │
│   ├── rules/
│   │   ├── rule-engine.js             Glob/method/header/content-type pattern matching
│   │   └── rule-store.js              CRUD for intercept rules (persisted to JSON)
│   │
│   └── chat/
│       ├── chat-handler.js            WebSocket chat message router
│       ├── claude-session.js           Claude CLI subprocess manager
│       └── context-builder.js          System prompt assembly + token estimation
│
├── static/
│   ├── index.html                     Single-page dashboard shell
│   ├── app.js                         All UI logic (vanilla JS IIFE)
│   ├── app.css                        Dark theme (Linear-inspired)
│   ├── chat.js                        AI chat panel UI (vanilla JS IIFE)
│   └── chat.css                       Chat panel styles
│
├── extension/                         Chrome MV3 extension (optional)
│   ├── manifest.json
│   ├── popup.html / popup.js
│
├── docs/                              Browsable HTML documentation
│   ├── index.html                     Documentation home
│   ├── getting-started.html           Installation & setup
│   ├── certificates.html              TLS & certificates deep dive
│   ├── dashboard.html                 Dashboard usage guide
│   ├── interception.html              Intercept & modify guide
│   ├── ai-chat.html                   AI chat agent guide
│   ├── architecture.html              Full architecture breakdown
│   ├── request-lifecycle.html         Request flow diagrams
│   ├── api-reference.html             REST & WebSocket API reference
│   └── docs.css                       Documentation styles
│
├── certs/                             Generated CA + per-host certs (gitignored)
└── sessions/                          Saved traffic captures (gitignored)
```

## REST API Reference

All endpoints are served by the dashboard on `:9081`.

### Traffic

| Method   | Path                         | Description                          |
|----------|------------------------------|--------------------------------------|
| `GET`    | `/api/traffic`               | List all traffic summaries           |
| `GET`    | `/api/traffic/:id`           | Full detail for one entry            |
| `DELETE` | `/api/traffic`               | Clear all traffic                    |
| `POST`   | `/api/traffic/:id/forward`   | Forward an intercepted request       |
| `POST`   | `/api/traffic/:id/drop`      | Drop an intercepted request          |
| `POST`   | `/api/traffic/:id/forward-response` | Forward an intercepted response |
| `POST`   | `/api/traffic/:id/drop-response`    | Drop an intercepted response    |

### Intercept

| Method   | Path                         | Description                          |
|----------|------------------------------|--------------------------------------|
| `GET`    | `/api/intercept`             | Get intercept enabled state          |
| `POST`   | `/api/intercept`             | Toggle intercept `{ "enabled": true }` |

### Rules

| Method   | Path                         | Description                          |
|----------|------------------------------|--------------------------------------|
| `GET`    | `/api/rules`                 | List all rules                       |
| `POST`   | `/api/rules`                 | Create a rule                        |
| `PUT`    | `/api/rules/:id`             | Update a rule                        |
| `DELETE` | `/api/rules/:id`             | Delete a rule                        |

### Sessions & Export

| Method   | Path                           | Description                        |
|----------|--------------------------------|------------------------------------|
| `GET`    | `/api/sessions`                | List saved session files           |
| `POST`   | `/api/sessions`                | Save current traffic to file       |
| `POST`   | `/api/sessions/:file/load`     | Load a saved session               |
| `GET`    | `/api/export/har`              | Download traffic as HAR 1.2        |

### Chat

| Method   | Path                           | Description                        |
|----------|--------------------------------|------------------------------------|
| `GET`    | `/api/chat/status`             | AI chat agent availability + info  |
| `POST`   | `/api/chat/browser-context`    | Send browser cookies/localStorage  |

### WebSocket

Connect to `ws://localhost:9081`. Messages are JSON:

```json
// Traffic events (server → client)
{ "type": "init",   "count": 42 }
{ "type": "add",    "entry": { /* summary */ } }
{ "type": "update", "entry": { /* summary */ } }
{ "type": "clear" }

// Chat events (bidirectional) — see docs/api-reference.html for full protocol
{ "type": "chat:send",   "messageId": "...", "text": "...", "contextToggles": {...} }
{ "type": "chat:chunk",  "messageId": "...", "text": "..." }
{ "type": "chat:done",   "messageId": "...", "fullText": "..." }
{ "type": "chat:status", "available": true, "tokenEstimate": 1500 }
```

## Keyboard Shortcuts

| Key     | Action                              |
|---------|-------------------------------------|
| `j`     | Select next request                 |
| `k`     | Select previous request             |
| `f`     | Focus URL filter input              |
| `i`     | Toggle intercept on/off             |
| `Esc`   | Close any open modal                |

## Dashboard Body Rendering

The detail panel renders bodies based on content type:

| Content-Type              | Rendering                                         |
|---------------------------|----------------------------------------------------|
| `application/json`        | Pretty-printed, syntax highlighted, collapsible tree |
| `text/html`               | Syntax highlighted (tags, attributes, strings)     |
| `text/javascript`         | Syntax highlighted (keywords, strings, comments)   |
| `text/css`                | Syntax highlighted (properties, colors, comments)  |
| `image/*`                 | Inline image preview                               |
| Other text types          | Plain text                                         |
| Binary / unknown          | Hex dump (16 bytes/row with ASCII sidebar)         |

Compressed responses (gzip, deflate, brotli) are automatically decompressed before display.

## Technical Gotchas

### Two-port architecture is intentional

The proxy runs on `:9080` and the dashboard on `:9081`. If both were on the same port and your browser used the proxy, every dashboard request would create traffic entries of itself — an infinite feedback loop. Two ports keeps them cleanly separated.

### HTTPS requires trusting the CA certificate

Without trusting `certs/ca.crt`, browsers will show certificate errors for every HTTPS site. The proxy generates the CA once on first launch. If you delete `certs/`, a new CA is generated and you'll need to re-trust it.

### Body capture is capped at 2MB per entry

Request and response bodies are captured up to 2MB for display in the dashboard. The full payload is always forwarded to its destination regardless of size. This prevents memory exhaustion when proxying large file downloads.

### Only one request per HTTPS CONNECT tunnel

~~The TLS handler currently processes one HTTP request per CONNECT tunnel.~~ **Fixed.** The TLS handler now uses a proper HTTP stream parser with keep-alive support. Multiple HTTP/1.1 requests on the same CONNECT tunnel are parsed and captured individually. The parser handles both `Content-Length` and `Transfer-Encoding: chunked` to know when each message ends, then loops for the next request.

### HTTP/2 upstream with HTTP/1.1 client translation

When connecting to upstream HTTPS servers, the proxy first tries HTTP/2 via ALPN negotiation. If the server supports H2, the proxy uses HTTP/2 streams for the upstream connection but translates responses back to HTTP/1.1 before sending them through the CONNECT tunnel to the client. You'll see an `h2` badge in the traffic list for requests served via HTTP/2. If the server doesn't support H2, the proxy falls back to HTTP/1.1 transparently.

### Response interception buffers the full response

When response interception is enabled and a rule matches, the proxy buffers the complete upstream response body before showing it to the user. This means the client doesn't receive any data until you click Forward/Drop. For large responses, this increases memory usage temporarily. The 5-minute auto-forward timeout also applies to intercepted responses.

### Intercepted requests have a 5-minute timeout

If you intercept a request and forget to forward or drop it, it auto-forwards after 5 minutes. This prevents the client from hanging indefinitely and leaking connections.

### The ring buffer evicts old entries silently

The traffic store holds 5000 entries. When full, the oldest entry is removed to make room. There's no warning — save your session before it fills up if you need to preserve everything.

### Header names are lowercased

Node's `http` module lowercases all header names. The proxy preserves this behavior — you'll see `content-type` not `Content-Type` in the dashboard. This is standard HTTP/1.1 behavior (headers are case-insensitive) but may look different from what browser dev tools show.

### `node-forge` RSA key generation is slow on first hit

The first HTTPS request to a new hostname takes ~200–500ms because `node-forge` generates a 2048-bit RSA keypair in pure JavaScript. Subsequent requests to the same host use the cached cert from `certs/hosts/` and are instantaneous.

### HAR export uses UTF-8 text representation

Binary response bodies in the HAR export are represented as UTF-8 strings, which may not round-trip correctly for binary content. This is a known limitation — the HAR format doesn't have great binary support. For binary analysis, use the hex dump view in the dashboard instead.

## Potential Improvements

### High Value

- ~~**HTTP/2 support**~~ **Done.** Upstream H2 via ALPN negotiation, translated back to H1 for clients.
- ~~**Keep-alive over CONNECT tunnels**~~ **Done.** Proper HTTP stream parser with chunked + content-length awareness.
- ~~**Response interception**~~ **Done.** Full hold/edit/forward/drop for responses, with phase-aware UI.
- **WebSocket traffic inspection** — After a WebSocket upgrade, frames are currently tunneled blindly. Parsing WebSocket frames would show real-time message content in the dashboard.
- ~~**Chunked transfer-encoding awareness**~~ **Done.** Both request and response bodies parsed correctly.

### Medium Value

- **Virtual scrolling** — The traffic list renders all DOM rows. At 5000 entries this is fine, but virtual scrolling would reduce memory and improve paint performance for very long sessions.
- **Programmatic rule API via CLI** — Allow adding intercept rules from the command line (`node server.js --rule "POST *login*"`) without opening the dashboard.
- **Diff view for modified requests** — When a request is intercepted and modified, show a before/after diff in the detail panel.
- **Request replay** — Right-click a traffic entry to replay the request. Useful for testing API endpoints repeatedly with different parameters.
- **Search across request/response bodies** — The current filter searches URLs only. Full-text search across captured bodies would help find specific payloads.

### Nice to Have

- **Multiple highlight themes** — The CSS variables make this easy to add (light mode, Solarized, etc.).
- **cURL export** — Generate a `curl` command from any captured request, ready to paste into a terminal.
- **Connection pooling visualization** — Show which requests share the same TCP connection.
- **Certificate pinning bypass notes** — Document how to handle apps that pin certificates (iOS/Android trust store injection, etc.).
- **Plugin/hook system** — Allow user-defined JavaScript functions that transform requests/responses automatically (e.g., always inject an auth header).
