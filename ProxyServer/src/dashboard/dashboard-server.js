const http = require('http');
const fs = require('fs');
const path = require('path');

const STATIC_DIR = path.join(__dirname, '..', '..', 'static');
const SESSIONS_DIR = path.join(__dirname, '..', '..', 'sessions');
const MIME_TYPES = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

class DashboardServer {
  constructor({ port = 8081, trafficStore }) {
    this.port = port;
    this.trafficStore = trafficStore;
    this.server = null;
    this.ruleStore = null;
    this.requestInterceptor = null;
    this.responseInterceptor = null;
    this.chatHandler = null;
  }

  start() {
    this.server = http.createServer((req, res) => this._handleRequest(req, res));
    this.server.on('error', err => console.error('[Dashboard] Server error:', err.message));

    return new Promise(resolve => {
      this.server.listen(this.port, () => {
        console.log(`[Dashboard] Web UI at http://localhost:${this.port}`);
        resolve();
      });
    });
  }

  stop() {
    return new Promise(resolve => {
      if (this.server) this.server.close(resolve);
      else resolve();
    });
  }

  _handleRequest(req, res) {
    const parsed = new URL(req.url, `http://localhost:${this.port}`);
    const pathname = parsed.pathname;

    if (pathname.startsWith('/api/')) {
      return this._handleAPI(req, res, pathname, parsed);
    }

    // Static file serving
    let filePath = pathname === '/' ? '/index.html' : pathname;
    filePath = path.join(STATIC_DIR, filePath);

    if (!filePath.startsWith(STATIC_DIR)) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }

    const ext = path.extname(filePath);
    const contentType = MIME_TYPES[ext] || 'application/octet-stream';

    fs.readFile(filePath, (err, data) => {
      if (err) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
        return;
      }
      res.writeHead(200, { 'Content-Type': contentType });
      res.end(data);
    });
  }

  _handleAPI(req, res, pathname, parsed) {
    res.setHeader('Content-Type', 'application/json');

    // === Traffic API ===

    if (pathname === '/api/traffic' && req.method === 'GET') {
      res.writeHead(200);
      res.end(JSON.stringify(this.trafficStore.getAllSummaries()));
      return;
    }

    if (pathname === '/api/traffic' && req.method === 'DELETE') {
      this.trafficStore.clear();
      res.writeHead(200);
      res.end(JSON.stringify({ ok: true }));
      return;
    }

    const detailMatch = pathname.match(/^\/api\/traffic\/([^/]+)$/);
    if (detailMatch && req.method === 'GET') {
      const entry = this.trafficStore.get(detailMatch[1]);
      if (!entry) { this._notFound(res); return; }
      res.writeHead(200);
      res.end(JSON.stringify(entry.toJSON()));
      return;
    }

    const forwardMatch = pathname.match(/^\/api\/traffic\/([^/]+)\/forward$/);
    if (forwardMatch && req.method === 'POST') {
      return this._readBody(req, (body) => {
        const entry = this.trafficStore.get(forwardMatch[1]);
        if (!entry) { this._notFound(res); return; }
        try {
          const modifications = body ? JSON.parse(body) : {};
          this.trafficStore.emit('forward', entry.id, modifications);
          this._ok(res);
        } catch (e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    const dropMatch = pathname.match(/^\/api\/traffic\/([^/]+)\/drop$/);
    if (dropMatch && req.method === 'POST') {
      const entry = this.trafficStore.get(dropMatch[1]);
      if (!entry) { this._notFound(res); return; }
      this.trafficStore.emit('drop', entry.id);
      this._ok(res);
      return;
    }

    // POST /api/traffic/:id/forward-response - forward an intercepted response
    const forwardResMatch = pathname.match(/^\/api\/traffic\/([^/]+)\/forward-response$/);
    if (forwardResMatch && req.method === 'POST') {
      return this._readBody(req, (body) => {
        const entry = this.trafficStore.get(forwardResMatch[1]);
        if (!entry) { this._notFound(res); return; }
        try {
          const modifications = body ? JSON.parse(body) : {};
          this.trafficStore.emit('forward-response', entry.id, modifications);
          this._ok(res);
        } catch (e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    // POST /api/traffic/:id/drop-response - drop an intercepted response
    const dropResMatch = pathname.match(/^\/api\/traffic\/([^/]+)\/drop-response$/);
    if (dropResMatch && req.method === 'POST') {
      const entry = this.trafficStore.get(dropResMatch[1]);
      if (!entry) { this._notFound(res); return; }
      this.trafficStore.emit('drop-response', entry.id);
      this._ok(res);
      return;
    }

    // === Intercept Toggle ===

    if (pathname === '/api/intercept' && req.method === 'GET') {
      res.writeHead(200);
      res.end(JSON.stringify({
        requestEnabled: this.requestInterceptor ? this.requestInterceptor.enabled : false,
        responseEnabled: this.responseInterceptor ? this.responseInterceptor.enabled : false,
      }));
      return;
    }

    if (pathname === '/api/intercept' && req.method === 'POST') {
      return this._readBody(req, (body) => {
        try {
          const { enabled } = JSON.parse(body);
          if (this.requestInterceptor) this.requestInterceptor.enabled = !!enabled;
          if (this.responseInterceptor) this.responseInterceptor.enabled = !!enabled;
          this._ok(res);
        } catch(e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    // === Rules API ===

    if (pathname === '/api/rules' && req.method === 'GET') {
      res.writeHead(200);
      res.end(JSON.stringify(this.ruleStore ? this.ruleStore.getAll() : []));
      return;
    }

    if (pathname === '/api/rules' && req.method === 'POST') {
      return this._readBody(req, (body) => {
        if (!this.ruleStore) { this._notFound(res); return; }
        try {
          const rule = JSON.parse(body);
          const created = this.ruleStore.add(rule);
          res.writeHead(201);
          res.end(JSON.stringify(created));
        } catch(e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    const ruleMatch = pathname.match(/^\/api\/rules\/([^/]+)$/);
    if (ruleMatch && req.method === 'PUT') {
      return this._readBody(req, (body) => {
        if (!this.ruleStore) { this._notFound(res); return; }
        try {
          const updates = JSON.parse(body);
          const updated = this.ruleStore.update(ruleMatch[1], updates);
          if (!updated) { this._notFound(res); return; }
          res.writeHead(200);
          res.end(JSON.stringify(updated));
        } catch(e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    if (ruleMatch && req.method === 'DELETE') {
      if (!this.ruleStore) { this._notFound(res); return; }
      this.ruleStore.remove(ruleMatch[1]);
      this._ok(res);
      return;
    }

    // === Sessions API ===

    if (pathname === '/api/sessions' && req.method === 'GET') {
      fs.mkdirSync(SESSIONS_DIR, { recursive: true });
      try {
        const files = fs.readdirSync(SESSIONS_DIR).filter(f => f.endsWith('.json')).sort().reverse();
        res.writeHead(200);
        res.end(JSON.stringify(files));
      } catch(e) {
        res.writeHead(200);
        res.end(JSON.stringify([]));
      }
      return;
    }

    if (pathname === '/api/sessions' && req.method === 'POST') {
      fs.mkdirSync(SESSIONS_DIR, { recursive: true });
      const entries = this.trafficStore.getAll().map(e => e.toJSON());
      const filename = `session-${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.json`;
      const filePath = path.join(SESSIONS_DIR, filename);
      fs.writeFileSync(filePath, JSON.stringify(entries, null, 2));
      res.writeHead(200);
      res.end(JSON.stringify({ file: filename }));
      return;
    }

    const loadMatch = pathname.match(/^\/api\/sessions\/([^/]+)\/load$/);
    if (loadMatch && req.method === 'POST') {
      const filePath = path.join(SESSIONS_DIR, loadMatch[1]);
      if (!filePath.startsWith(SESSIONS_DIR) || !fs.existsSync(filePath)) {
        this._notFound(res);
        return;
      }
      try {
        const TrafficEntry = require('../traffic/traffic-entry');
        const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
        this.trafficStore.clear();
        for (const item of data) {
          const entry = new TrafficEntry({
            method: item.request.method,
            url: item.request.url,
            httpVersion: item.request.httpVersion,
            headers: item.request.headers,
            protocol: item.target.protocol,
            host: item.target.host,
            port: item.target.port,
          });
          entry.id = item.id;
          entry.state = item.state;
          entry.timing = item.timing;
          entry.intercept = item.intercept;
          if (item.request.body) entry.request.body = Buffer.from(item.request.body, 'base64');
          if (item.response.statusCode) {
            entry.setResponse({
              statusCode: item.response.statusCode,
              statusMessage: item.response.statusMessage,
              headers: item.response.headers,
            });
          }
          if (item.response.body) entry.response.body = Buffer.from(item.response.body, 'base64');
          this.trafficStore.add(entry);
        }
        this._ok(res);
      } catch(e) {
        res.writeHead(500);
        res.end(JSON.stringify({ error: e.message }));
      }
      return;
    }

    // === HAR Export ===

    if (pathname === '/api/export/har' && req.method === 'GET') {
      const harExport = require('../traffic/har-export');
      const har = harExport(this.trafficStore.getAll());
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Content-Disposition', 'attachment; filename="capture.har"');
      res.writeHead(200);
      res.end(JSON.stringify(har, null, 2));
      return;
    }

    // === Chat API ===

    if (pathname === '/api/chat/browser-context' && req.method === 'POST') {
      return this._readBody(req, (body) => {
        try {
          const data = JSON.parse(body);
          if (this.chatHandler) {
            this.chatHandler.contextBuilder.setBrowserContext(data);
          }
          this._ok(res);
        } catch (e) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Invalid JSON' }));
        }
      });
    }

    if (pathname === '/api/chat/status' && req.method === 'GET') {
      if (this.chatHandler) {
        const session = this.chatHandler._getSession();
        const toggles = { includeTraffic: true, includeSelected: false, includeRules: false, includeSource: false, includeBrowser: false };
        const { tokenEstimate, breakdown } = this.chatHandler.contextBuilder.buildSystemPrompt(toggles);
        res.writeHead(200);
        res.end(JSON.stringify({
          available: session.isAvailable(),
          sessionActive: session.history.length > 0,
          contextToggles: toggles,
          tokenEstimate,
          breakdown,
        }));
      } else {
        res.writeHead(200);
        res.end(JSON.stringify({ available: false, sessionActive: false }));
      }
      return;
    }

    this._notFound(res);
  }

  _readBody(req, cb) {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => cb(body));
  }

  _ok(res) {
    res.writeHead(200);
    res.end(JSON.stringify({ ok: true }));
  }

  _notFound(res) {
    res.writeHead(404);
    res.end(JSON.stringify({ error: 'Not found' }));
  }
}

module.exports = DashboardServer;
