const fs = require('fs');
const path = require('path');

const PREAMBLE = `You are an AI assistant embedded in a ProxyServer dashboard.

## ProxyServer Architecture
- **Proxy** (default :9080): HTTP/HTTPS MITM proxy that intercepts, inspects, and optionally modifies traffic
- **Dashboard** (default :9081): Web UI + REST API + WebSocket for real-time traffic viewing

## Key Modules
- server.js — orchestrator, wires all components
- src/proxy/proxy-server.js — core HTTP proxy, CONNECT tunneling
- src/proxy/tls-handler.js — TLS MITM with on-the-fly cert generation
- src/proxy/cert-manager.js — CA and per-host certificate management
- src/proxy/request-interceptor.js — pauses matching requests for user review
- src/proxy/response-interceptor.js — pauses matching responses for user review
- src/rules/rule-engine.js — evaluates intercept rules against traffic
- src/rules/rule-store.js — persistent rule storage (rules.json)
- src/traffic/traffic-store.js — in-memory ring buffer (max 5000 entries)
- src/traffic/traffic-entry.js — single request/response lifecycle object
- src/traffic/har-export.js — HAR format export
- src/dashboard/dashboard-server.js — static file server + REST API
- src/dashboard/ws-bridge.js — WebSocket bridge for real-time updates

## REST API (all on dashboard port)
- GET/DELETE /api/traffic — list/clear traffic
- GET /api/traffic/:id — full entry detail
- POST /api/traffic/:id/forward — forward intercepted request with modifications
- POST /api/traffic/:id/drop — drop intercepted request
- POST /api/traffic/:id/forward-response — forward intercepted response
- POST /api/traffic/:id/drop-response — drop intercepted response
- GET/POST /api/intercept — check/toggle intercept
- GET/POST/PUT/DELETE /api/rules[/:id] — CRUD rules
- GET/POST /api/sessions — list/save sessions
- POST /api/sessions/:filename/load — load session
- GET /api/export/har — export as HAR

## Your Capabilities
You have full tool access (Read, Write, Edit, Bash) scoped to the ProxyServer directory.
You can modify source code, create intercept rules via the REST API, generate curl commands, and analyze captured traffic.
When creating rules or modifying traffic, use the REST API endpoints listed above via curl or fetch.
`;

const SOURCE_FILES = [
  'server.js',
  'src/proxy/proxy-server.js',
  'src/proxy/tls-handler.js',
  'src/dashboard/dashboard-server.js',
  'src/traffic/traffic-entry.js',
  'src/traffic/traffic-store.js',
  'src/rules/rule-engine.js',
  'src/rules/rule-store.js',
  'src/proxy/request-interceptor.js',
  'src/proxy/response-interceptor.js',
];

class ContextBuilder {
  constructor({ trafficStore, ruleStore, projectRoot }) {
    this.trafficStore = trafficStore;
    this.ruleStore = ruleStore;
    this.projectRoot = projectRoot;
    this._sourceCache = new Map();
    this._watchers = new Map();
    this._browserContext = null;
    this._initSourceWatchers();
  }

  _initSourceWatchers() {
    for (const relPath of SOURCE_FILES) {
      const absPath = path.join(this.projectRoot, relPath);
      try {
        // Pre-cache
        this._sourceCache.set(relPath, fs.readFileSync(absPath, 'utf8'));
        // Watch for changes
        const watcher = fs.watch(absPath, () => {
          try {
            this._sourceCache.set(relPath, fs.readFileSync(absPath, 'utf8'));
          } catch (e) {
            this._sourceCache.delete(relPath);
          }
        });
        this._watchers.set(relPath, watcher);
      } catch (e) {
        // File may not exist yet
      }
    }
  }

  setBrowserContext(data) {
    this._browserContext = data;
  }

  /**
   * Returns the static preamble used as --system-prompt on the first message.
   */
  buildStaticPreamble() {
    return { prompt: PREAMBLE, tokenEstimate: this._estimateTokens(PREAMBLE) };
  }

  /**
   * Returns only the dynamic/changing context (traffic, selected entry, rules, etc.)
   * to be prepended to user messages on each send.
   */
  buildDynamicContext(options = {}) {
    const {
      includeTraffic = true,
      includeSelected = false,
      includeRules = false,
      includeSource = false,
      includeBrowser = false,
      selectedEntryId = null,
    } = options;

    const blocks = [];
    const breakdown = {};

    // Traffic summaries
    if (includeTraffic) {
      const trafficBlock = this._buildTrafficBlock();
      if (trafficBlock) {
        blocks.push(trafficBlock);
        breakdown.traffic = this._estimateTokens(trafficBlock);
      } else {
        breakdown.traffic = 0;
      }
    } else {
      breakdown.traffic = 0;
    }

    // Selected entry detail
    if (includeSelected && selectedEntryId) {
      const selectedBlock = this._buildSelectedBlock(selectedEntryId);
      if (selectedBlock) {
        blocks.push(selectedBlock);
        breakdown.selected = this._estimateTokens(selectedBlock);
      } else {
        breakdown.selected = 0;
      }
    } else {
      breakdown.selected = 0;
    }

    // Intercept rules
    if (includeRules && this.ruleStore) {
      const rulesBlock = this._buildRulesBlock();
      blocks.push(rulesBlock);
      breakdown.rules = this._estimateTokens(rulesBlock);
    } else {
      breakdown.rules = 0;
    }

    // Source code
    if (includeSource) {
      const sourceBlock = this._buildSourceBlock();
      if (sourceBlock) {
        blocks.push(sourceBlock);
        breakdown.source = this._estimateTokens(sourceBlock);
      } else {
        breakdown.source = 0;
      }
    } else {
      breakdown.source = 0;
    }

    // Browser context
    if (includeBrowser && this._browserContext) {
      const browserBlock = this._buildBrowserBlock();
      blocks.push(browserBlock);
      breakdown.browser = this._estimateTokens(browserBlock);
    } else {
      breakdown.browser = 0;
    }

    const prompt = blocks.length > 0 ? blocks.join('\n\n') : '';
    const tokenEstimate = Object.values(breakdown).reduce((a, b) => a + b, 0);

    return { prompt, tokenEstimate, breakdown };
  }

  /**
   * Full system prompt (backward compat for status display).
   * Composes from static preamble + dynamic context.
   */
  buildSystemPrompt(options = {}) {
    const { prompt: preamble, tokenEstimate: preambleTokens } = this.buildStaticPreamble();
    const { prompt: dynamic, tokenEstimate: dynamicTokens, breakdown: dynamicBreakdown } = this.buildDynamicContext(options);

    const breakdown = { preamble: preambleTokens, ...dynamicBreakdown };
    const parts = [preamble];
    if (dynamic) parts.push(dynamic);
    const prompt = parts.join('\n\n');
    const tokenEstimate = Object.values(breakdown).reduce((a, b) => a + b, 0);

    return { prompt, tokenEstimate, breakdown };
  }

  // --- Private block builders (shared by buildDynamicContext and legacy buildSystemPrompt) ---

  _buildTrafficBlock() {
    const summaries = this.trafficStore.getAllSummaries().slice(-200);
    if (summaries.length === 0) return null;
    const header = '# | Method | Status | Host | Path | Type | Size | Time';
    const divider = '---|--------|--------|------|------|------|------|-----';
    const rows = summaries.map(s => {
      const urlPath = this._extractPath(s.url);
      const ct = this._shortContentType(s.contentType);
      const size = s.responseBodySize || '-';
      const time = s.duration ? s.duration + 'ms' : '-';
      return `${s.seq} | ${s.method} | ${s.statusCode || '-'} | ${s.host || ''} | ${urlPath} | ${ct} | ${size} | ${time}`;
    });
    const table = [header, divider, ...rows].join('\n');
    return `<traffic-summaries>\n${summaries.length} captured requests (most recent 200 shown):\n${table}\n</traffic-summaries>`;
  }

  _buildSelectedBlock(selectedEntryId) {
    const entry = this.trafficStore.get(selectedEntryId);
    if (!entry) return null;
    let detail;
    try {
      detail = JSON.stringify(entry.toJSON(), null, 2);
      if (detail.length > 25000) {
        const json = entry.toJSON();
        if (json.request && json.request.body && json.request.body.length > 13333) {
          json.request.body = json.request.body.slice(0, 13333) + '...(truncated)';
        }
        if (json.response && json.response.body && json.response.body.length > 13333) {
          json.response.body = json.response.body.slice(0, 13333) + '...(truncated)';
        }
        detail = JSON.stringify(json, null, 2);
      }
    } catch (e) {
      detail = '(error serializing entry)';
    }
    return `<selected-entry-detail>\nCurrently selected traffic entry (id: ${selectedEntryId}):\n${detail}\n</selected-entry-detail>`;
  }

  _buildRulesBlock() {
    const rules = this.ruleStore.getAll();
    return `<intercept-rules>\n${rules.length} intercept rules:\n${JSON.stringify(rules, null, 2)}\n</intercept-rules>`;
  }

  _buildSourceBlock() {
    const parts = [];
    for (const relPath of SOURCE_FILES) {
      const content = this._sourceCache.get(relPath);
      if (content) {
        parts.push(`--- ${relPath} ---\n${content}`);
      }
    }
    if (parts.length === 0) return null;
    return `<source-code>\nKey source files from ProxyServer:\n${parts.join('\n\n')}\n</source-code>`;
  }

  _buildBrowserBlock() {
    const bc = this._browserContext;
    return `<browser-context>\nURL: ${bc.url || 'unknown'}\nCookies: ${JSON.stringify(bc.cookies || [], null, 2)}\nLocalStorage keys: ${JSON.stringify(Object.keys(bc.localStorage || {}))}\n</browser-context>`;
  }

  _estimateTokens(text) {
    // ~4 chars per token is a reasonable approximation
    return Math.ceil(text.length / 4);
  }

  _extractPath(url) {
    if (!url) return '';
    try {
      const u = new URL(url);
      return u.pathname + u.search;
    } catch (e) {
      return url;
    }
  }

  _shortContentType(ct) {
    if (!ct) return '';
    if (ct.includes('json')) return 'JSON';
    if (ct.includes('html')) return 'HTML';
    if (ct.includes('javascript')) return 'JS';
    if (ct.includes('css')) return 'CSS';
    if (ct.includes('image/')) return 'IMG';
    if (ct.includes('xml')) return 'XML';
    return ct.split('/').pop().split(';')[0].slice(0, 8);
  }

  destroy() {
    for (const watcher of this._watchers.values()) {
      watcher.close();
    }
    this._watchers.clear();
  }
}

module.exports = ContextBuilder;
