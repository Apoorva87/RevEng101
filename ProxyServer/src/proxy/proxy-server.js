const http = require('http');
const url = require('url');
const zlib = require('zlib');
const TrafficEntry = require('../traffic/traffic-entry');

class ProxyServer {
  constructor({ port = 8080, trafficStore, requestInterceptor, responseInterceptor }) {
    this.port = port;
    this.trafficStore = trafficStore;
    this.requestInterceptor = requestInterceptor || null;
    this.responseInterceptor = responseInterceptor || null;
    this.server = null;
    this.tlsHandler = null;
  }

  start() {
    this.server = http.createServer((req, res) => this._handleRequest(req, res));
    this.server.on('connect', (req, clientSocket, head) => this._handleConnect(req, clientSocket, head));
    this.server.on('error', err => console.error('[Proxy] Server error:', err.message));

    return new Promise(resolve => {
      this.server.listen(this.port, () => {
        console.log(`[Proxy] HTTP proxy listening on :${this.port}`);
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

  _handleRequest(clientReq, clientRes) {
    const parsed = url.parse(clientReq.url);
    const isAbsolute = parsed.protocol && parsed.hostname;

    if (!isAbsolute) {
      clientRes.writeHead(400, { 'Content-Type': 'text/plain' });
      clientRes.end('Bad Request: proxy requires absolute URI');
      return;
    }

    const targetHost = parsed.hostname;
    const targetPort = parseInt(parsed.port, 10) || 80;
    const targetPath = parsed.path;

    const entry = new TrafficEntry({
      method: clientReq.method,
      url: clientReq.url,
      httpVersion: clientReq.httpVersion,
      headers: { ...clientReq.headers },
      clientIp: clientReq.socket.remoteAddress,
      clientPort: clientReq.socket.remotePort,
      protocol: 'http',
      host: targetHost,
      port: targetPort,
    });
    this.trafficStore.add(entry);

    // Collect request body
    const bodyChunks = [];
    let bodyLen = 0;
    clientReq.on('data', chunk => {
      bodyLen += chunk.length;
      if (bodyLen <= TrafficEntry.MAX_BODY_CAPTURE) {
        bodyChunks.push(chunk);
      }
    });

    clientReq.on('end', async () => {
      if (bodyChunks.length > 0) {
        entry.setRequestBody(Buffer.concat(bodyChunks));
        this.trafficStore.update(entry.id);
      }

      // Request interception
      let reqHeaders = { ...clientReq.headers };
      let reqBody = bodyChunks.length > 0 ? Buffer.concat(bodyChunks) : null;
      let method = clientReq.method;
      let path = targetPath;

      if (this.requestInterceptor) {
        try {
          const result = await this.requestInterceptor.checkRequest(entry, { method, path, headers: reqHeaders, body: reqBody });
          if (result) {
            method = result.method || method;
            path = result.path || path;
            reqHeaders = result.headers || reqHeaders;
            reqBody = result.body != null ? Buffer.from(result.body) : reqBody;
          }
        } catch (e) {
          entry.abort();
          this.trafficStore.update(entry.id);
          clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
          clientRes.end('Proxy intercept aborted');
          return;
        }
      }

      delete reqHeaders['proxy-connection'];
      delete reqHeaders['proxy-authorization'];

      entry.state = TrafficEntry.STATES.FORWARDED;
      this.trafficStore.update(entry.id);

      const proxyReqOptions = {
        hostname: targetHost,
        port: targetPort,
        path: path,
        method: method,
        headers: reqHeaders,
      };

      const proxyReq = http.request(proxyReqOptions, (proxyRes) => {
        entry.setResponse({
          statusCode: proxyRes.statusCode,
          statusMessage: proxyRes.statusMessage,
          headers: { ...proxyRes.headers },
          httpVersion: proxyRes.httpVersion || '1.1',
        });
        this.trafficStore.update(entry.id);

        // Collect FULL response body before deciding whether to intercept
        const rawChunks = [];
        let rawLen = 0;
        proxyRes.on('data', chunk => {
          rawLen += chunk.length;
          rawChunks.push(chunk);
        });

        proxyRes.on('end', async () => {
          const rawBody = Buffer.concat(rawChunks);

          // Decompress for display
          const decompressedBody = await this._decompressBody(rawBody, proxyRes.headers['content-encoding']);
          if (decompressedBody && decompressedBody.length > 0) {
            entry.setResponseBody(decompressedBody);
            this.trafficStore.update(entry.id);
          }

          // Response interception
          if (this.responseInterceptor) {
            try {
              const resModifications = await this.responseInterceptor.checkResponse(entry, {
                statusCode: proxyRes.statusCode,
                headers: { ...proxyRes.headers },
                body: decompressedBody,
              });
              if (resModifications) {
                entry.intercept.wasModified = true;
                this.trafficStore.update(entry.id);
                // Send modified response to client
                const modHeaders = resModifications.headers || proxyRes.headers;
                const modBody = resModifications.body != null ? Buffer.from(resModifications.body) : rawBody;
                const modStatus = resModifications.statusCode || proxyRes.statusCode;
                // Update content-length for modified body
                const finalHeaders = { ...modHeaders };
                delete finalHeaders['transfer-encoding'];
                finalHeaders['content-length'] = String(modBody.length);
                clientRes.writeHead(modStatus, finalHeaders);
                clientRes.end(modBody);
                entry.complete();
                this.trafficStore.update(entry.id);
                return;
              }
            } catch (e) {
              // Response dropped
              entry.abort();
              this.trafficStore.update(entry.id);
              clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
              clientRes.end('Response dropped by proxy');
              return;
            }
          }

          // Forward original response to client
          clientRes.writeHead(proxyRes.statusCode, proxyRes.headers);
          clientRes.end(rawBody);
          entry.complete();
          this.trafficStore.update(entry.id);
        });
      });

      proxyReq.on('error', err => {
        entry.fail(err.message);
        this.trafficStore.update(entry.id);
        if (!clientRes.headersSent) {
          clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
        }
        clientRes.end(`Proxy error: ${err.message}`);
      });

      if (reqBody) {
        proxyReq.write(reqBody);
      }
      proxyReq.end();
    });

    clientReq.on('error', err => {
      entry.fail(err.message);
      this.trafficStore.update(entry.id);
    });
  }

  _handleConnect(req, clientSocket, head) {
    const [host, portStr] = req.url.split(':');
    const port = parseInt(portStr, 10) || 443;

    if (this.tlsHandler) {
      this.tlsHandler.handleConnect(req, clientSocket, head, host, port);
      return;
    }

    // Default: blind TCP tunnel
    const net = require('net');
    const upstream = net.connect(port, host, () => {
      clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      upstream.write(head);
      upstream.pipe(clientSocket);
      clientSocket.pipe(upstream);
    });

    upstream.on('error', () => {
      clientSocket.end('HTTP/1.1 502 Bad Gateway\r\n\r\n');
    });
    clientSocket.on('error', () => upstream.destroy());
  }

  async _decompressBody(body, encoding) {
    if (!body || body.length === 0 || !encoding) return body;
    const enc = encoding.toLowerCase();
    try {
      if (enc === 'gzip' || enc === 'x-gzip') {
        return await promiseZlib(zlib.gunzip, body);
      } else if (enc === 'deflate') {
        return await promiseZlib(zlib.inflate, body);
      } else if (enc === 'br') {
        return await promiseZlib(zlib.brotliDecompress, body);
      }
    } catch (e) {
      // Decompression failed — return raw
    }
    return body;
  }
}

function promiseZlib(fn, buf) {
  return new Promise((resolve, reject) => {
    fn(buf, (err, result) => err ? reject(err) : resolve(result));
  });
}

module.exports = ProxyServer;
