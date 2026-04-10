const tls = require('tls');
const http2 = require('http2');
const zlib = require('zlib');
const TrafficEntry = require('../traffic/traffic-entry');

class TLSHandler {
  constructor({ certManager, trafficStore, requestInterceptor, responseInterceptor }) {
    this.certManager = certManager;
    this.trafficStore = trafficStore;
    this.requestInterceptor = requestInterceptor || null;
    this.responseInterceptor = responseInterceptor || null;
  }

  handleConnect(req, clientSocket, head, hostname, port) {
    const { key, cert: certPem } = this.certManager.getHostCert(hostname);

    clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');

    const tlsServer = new tls.TLSSocket(clientSocket, {
      isServer: true,
      key: key,
      cert: certPem,
    });

    tlsServer.on('error', (err) => {
      if (err.code === 'ECONNRESET' || err.code === 'EPIPE') return;
      if (err.message && err.message.includes('SSL')) return;
    });
    clientSocket.on('error', () => tlsServer.destroy());

    this._handleTunnelStream(tlsServer, hostname, port, clientSocket);
  }

  // Main loop: parse HTTP/1.1 requests from the decrypted tunnel, one after another (keep-alive)
  _handleTunnelStream(tlsServer, hostname, port, clientSocket) {
    const parser = new HTTPStreamParser(tlsServer);

    const processNextRequest = async () => {
      let reqMsg;
      try {
        reqMsg = await parser.readRequest();
      } catch (e) {
        // Connection closed or parse error — end of tunnel
        tlsServer.destroy();
        return;
      }

      if (!reqMsg) {
        tlsServer.destroy();
        return;
      }

      const fullUrl = `https://${hostname}${port !== 443 ? ':' + port : ''}${reqMsg.path}`;

      const entry = new TrafficEntry({
        method: reqMsg.method,
        url: fullUrl,
        httpVersion: reqMsg.httpVersion,
        headers: reqMsg.headers,
        clientIp: clientSocket.remoteAddress,
        clientPort: clientSocket.remotePort,
        protocol: 'https',
        host: hostname,
        port,
      });
      this.trafficStore.add(entry);

      if (reqMsg.body && reqMsg.body.length > 0) {
        entry.setRequestBody(reqMsg.body);
        this.trafficStore.update(entry.id);
      }

      // Request interception
      let method = reqMsg.method;
      let path = reqMsg.path;
      let headers = { ...reqMsg.headers };
      let body = reqMsg.body;

      if (this.requestInterceptor) {
        try {
          const result = await this.requestInterceptor.checkRequest(entry, { method, path, headers, body });
          if (result) {
            method = result.method || method;
            path = result.path || path;
            headers = result.headers || headers;
            body = result.body != null ? Buffer.from(result.body) : body;
          }
        } catch (e) {
          entry.abort();
          this.trafficStore.update(entry.id);
          tlsServer.destroy();
          return;
        }
      }

      entry.state = TrafficEntry.STATES.FORWARDED;
      this.trafficStore.update(entry.id);

      // Try H2 upstream, fall back to H1
      let resResult;
      try {
        resResult = await this._sendUpstreamRequest(hostname, port, method, path, headers, body);
      } catch (e) {
        entry.fail(e.message);
        this.trafficStore.update(entry.id);
        tlsServer.destroy();
        return;
      }

      const { statusCode, statusMessage, resHeaders, resBody, httpVersion: resHttpVersion } = resResult;

      entry.setResponse({
        statusCode,
        statusMessage,
        headers: resHeaders,
        httpVersion: resHttpVersion,
      });
      this.trafficStore.update(entry.id);

      // Decompress body for display
      const decompressedBody = await this._decompressBody(resBody, resHeaders['content-encoding']);
      if (decompressedBody && decompressedBody.length > 0) {
        entry.setResponseBody(decompressedBody);
      }

      // Response interception
      if (this.responseInterceptor) {
        try {
          const resModifications = await this.responseInterceptor.checkResponse(entry, {
            statusCode, headers: resHeaders, body: decompressedBody,
          });
          if (resModifications) {
            // User modified the response
            entry.intercept.wasModified = true;
            this.trafficStore.update(entry.id);
            // Rebuild response with modifications
            const modHeaders = resModifications.headers || resHeaders;
            const modBody = resModifications.body != null ? Buffer.from(resModifications.body) : resBody;
            const modStatus = resModifications.statusCode || statusCode;
            this._writeHTTP1Response(tlsServer, modStatus, statusMessage, modHeaders, modBody, resHttpVersion);
            entry.complete();
            this.trafficStore.update(entry.id);
            processNextRequest();
            return;
          }
        } catch (e) {
          // Response dropped
          entry.abort();
          this.trafficStore.update(entry.id);
          tlsServer.destroy();
          return;
        }
      }

      // Forward original response to client
      this._writeHTTP1Response(tlsServer, statusCode, statusMessage, resHeaders, resBody, resHttpVersion);
      entry.complete();
      this.trafficStore.update(entry.id);

      // Loop for keep-alive
      const connection = (resHeaders['connection'] || '').toLowerCase();
      if (connection === 'close') {
        tlsServer.end();
      } else {
        processNextRequest();
      }
    };

    processNextRequest();
  }

  // Reconstruct an HTTP/1.1 response to send back through the TLS tunnel to the client
  _writeHTTP1Response(tlsServer, statusCode, statusMessage, headers, body, httpVersion) {
    const version = (httpVersion === '2' || httpVersion === '2.0') ? '1.1' : (httpVersion || '1.1');
    let head = `HTTP/${version} ${statusCode} ${statusMessage || ''}\r\n`;
    for (const [k, v] of Object.entries(headers)) {
      // Skip H2 pseudo-headers
      if (k.startsWith(':')) continue;
      // When translating from H2, we have the original body — set correct content-length
      if (k.toLowerCase() === 'content-length') continue;
      // Strip transfer-encoding if we're sending the full body
      if (k.toLowerCase() === 'transfer-encoding') continue;
      head += `${k}: ${v}\r\n`;
    }
    if (body && body.length > 0) {
      head += `content-length: ${body.length}\r\n`;
    }
    head += '\r\n';

    try {
      tlsServer.write(head);
      if (body && body.length > 0) {
        tlsServer.write(body);
      }
    } catch (e) {
      // client disconnected
    }
  }

  // Try HTTP/2 with ALPN, fall back to HTTP/1.1
  async _sendUpstreamRequest(hostname, port, method, path, headers, body) {
    try {
      return await this._sendH2Request(hostname, port, method, path, headers, body);
    } catch (e) {
      // H2 failed (server doesn't support it, or ALPN fallback) — use H1
      return await this._sendH1Request(hostname, port, method, path, headers, body);
    }
  }

  // HTTP/2 upstream via node's http2 module
  _sendH2Request(hostname, port, method, path, headers, body) {
    return new Promise((resolve, reject) => {
      const session = http2.connect(`https://${hostname}:${port}`, {
        rejectUnauthorized: false,
        ALPNProtocols: ['h2'],
      });

      let settled = false;
      const settle = (fn) => (...args) => { if (!settled) { settled = true; fn(...args); } };

      const timeout = setTimeout(() => {
        session.destroy();
        settle(reject)(new Error('H2 connection timeout'));
      }, 10000);

      session.on('error', (err) => {
        clearTimeout(timeout);
        session.destroy();
        settle(reject)(err);
      });

      session.once('connect', () => {
        // Check if we actually got H2
        if (session.alpnProtocol !== 'h2') {
          clearTimeout(timeout);
          session.destroy();
          settle(reject)(new Error('Server did not negotiate h2'));
          return;
        }

        // Build H2 request headers — remove hop-by-hop headers
        const h2Headers = { ':method': method, ':path': path, ':scheme': 'https', ':authority': hostname };
        for (const [k, v] of Object.entries(headers)) {
          const lk = k.toLowerCase();
          if (lk === 'host' || lk === 'connection' || lk === 'transfer-encoding' ||
              lk === 'keep-alive' || lk === 'proxy-connection' || lk === 'upgrade') continue;
          h2Headers[lk] = v;
        }

        const req = session.request(h2Headers);
        const resChunks = [];

        req.on('response', (resHeaders) => {
          req.on('data', chunk => resChunks.push(chunk));
          req.on('end', () => {
            clearTimeout(timeout);
            const status = resHeaders[':status'] || 200;
            // Convert H2 headers to flat object, skip pseudo-headers
            const flatHeaders = {};
            for (const [k, v] of Object.entries(resHeaders)) {
              if (!k.startsWith(':')) flatHeaders[k] = v;
            }
            const resBody = Buffer.concat(resChunks);
            session.close();
            settle(resolve)({
              statusCode: status,
              statusMessage: '',
              resHeaders: flatHeaders,
              resBody,
              httpVersion: '2',
            });
          });
        });

        req.on('error', (err) => {
          clearTimeout(timeout);
          session.destroy();
          settle(reject)(err);
        });

        if (body && body.length > 0) {
          req.write(body);
        }
        req.end();
      });
    });
  }

  // HTTP/1.1 upstream via raw TLS socket
  _sendH1Request(hostname, port, method, path, headers, body) {
    return new Promise((resolve, reject) => {
      const upstream = tls.connect({
        host: hostname,
        port: port,
        servername: hostname,
        rejectUnauthorized: false,
      }, () => {
        let reqLine = `${method} ${path} HTTP/1.1\r\n`;
        for (const [k, v] of Object.entries(headers)) {
          reqLine += `${k}: ${v}\r\n`;
        }
        reqLine += '\r\n';
        upstream.write(reqLine);
        if (body && body.length > 0) {
          upstream.write(body);
        }
      });

      const responseParser = new HTTPStreamParser(upstream);

      responseParser.readResponse().then(resMsg => {
        upstream.destroy();
        resolve({
          statusCode: resMsg.statusCode,
          statusMessage: resMsg.statusMessage,
          resHeaders: resMsg.headers,
          resBody: resMsg.body || Buffer.alloc(0),
          httpVersion: resMsg.httpVersion || '1.1',
        });
      }).catch(err => {
        upstream.destroy();
        reject(err);
      });
    });
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

// ===== HTTP Stream Parser =====
// Reads HTTP/1.1 messages (request or response) from a stream,
// supporting Content-Length, Transfer-Encoding: chunked, and no-body responses.
class HTTPStreamParser {
  constructor(stream) {
    this.stream = stream;
    this.buffer = Buffer.alloc(0);
    this.destroyed = false;

    stream.on('close', () => { this.destroyed = true; });
    stream.on('error', () => { this.destroyed = true; });
  }

  // Read bytes from the stream until we have at least `n` bytes in the buffer
  _readAtLeast(n) {
    return new Promise((resolve, reject) => {
      if (this.destroyed) { reject(new Error('Stream closed')); return; }
      if (this.buffer.length >= n) { resolve(); return; }

      const onData = (chunk) => {
        this.buffer = Buffer.concat([this.buffer, chunk]);
        if (this.buffer.length >= n) {
          cleanup();
          resolve();
        }
      };
      const onEnd = () => { cleanup(); reject(new Error('Stream ended')); };
      const onError = (err) => { cleanup(); reject(err); };
      const onClose = () => { cleanup(); reject(new Error('Stream closed')); };
      const cleanup = () => {
        this.stream.removeListener('data', onData);
        this.stream.removeListener('end', onEnd);
        this.stream.removeListener('error', onError);
        this.stream.removeListener('close', onClose);
      };

      this.stream.on('data', onData);
      this.stream.on('end', onEnd);
      this.stream.on('error', onError);
      this.stream.on('close', onClose);
    });
  }

  // Read until delimiter found in buffer
  _readUntil(delimiter) {
    return new Promise((resolve, reject) => {
      if (this.destroyed) { reject(new Error('Stream closed')); return; }

      const delBuf = Buffer.from(delimiter);
      const check = () => {
        const idx = this.buffer.indexOf(delBuf);
        if (idx >= 0) return idx;
        return -1;
      };

      let idx = check();
      if (idx >= 0) { resolve(idx); return; }

      const onData = (chunk) => {
        this.buffer = Buffer.concat([this.buffer, chunk]);
        idx = check();
        if (idx >= 0) { cleanup(); resolve(idx); }
      };
      const onEnd = () => { cleanup(); reject(new Error('Stream ended before delimiter')); };
      const onError = (err) => { cleanup(); reject(err); };
      const onClose = () => { cleanup(); reject(new Error('Stream closed')); };
      const cleanup = () => {
        this.stream.removeListener('data', onData);
        this.stream.removeListener('end', onEnd);
        this.stream.removeListener('error', onError);
        this.stream.removeListener('close', onClose);
      };

      this.stream.on('data', onData);
      this.stream.on('end', onEnd);
      this.stream.on('error', onError);
      this.stream.on('close', onClose);
    });
  }

  _consume(n) {
    const chunk = this.buffer.slice(0, n);
    this.buffer = this.buffer.slice(n);
    return chunk;
  }

  _parseHeaders(headerStr) {
    const headers = {};
    const lines = headerStr.split('\r\n');
    for (const line of lines) {
      const colonIdx = line.indexOf(':');
      if (colonIdx > 0) {
        const key = line.slice(0, colonIdx).trim().toLowerCase();
        const val = line.slice(colonIdx + 1).trim();
        // Handle duplicate headers by joining with comma
        if (headers[key]) {
          headers[key] += ', ' + val;
        } else {
          headers[key] = val;
        }
      }
    }
    return headers;
  }

  // Read request body based on content-length or transfer-encoding
  async _readBody(headers) {
    const te = (headers['transfer-encoding'] || '').toLowerCase();
    const cl = headers['content-length'];

    if (te.includes('chunked')) {
      return await this._readChunkedBody();
    } else if (cl !== undefined) {
      const len = parseInt(cl, 10);
      if (isNaN(len) || len <= 0) return Buffer.alloc(0);
      const cappedLen = Math.min(len, TrafficEntry.MAX_BODY_CAPTURE + 1024); // read a bit extra for safety
      await this._readAtLeast(cappedLen > this.buffer.length ? cappedLen : this.buffer.length);
      // Take exactly `len` bytes if available
      const take = Math.min(len, this.buffer.length);
      return this._consume(take);
    }

    return Buffer.alloc(0);
  }

  // Read response body — responses can also close-delimit (no content-length, not chunked)
  async _readResponseBody(headers, statusCode) {
    // 1xx, 204, 304 have no body
    if (statusCode < 200 || statusCode === 204 || statusCode === 304) {
      return Buffer.alloc(0);
    }

    const te = (headers['transfer-encoding'] || '').toLowerCase();
    const cl = headers['content-length'];

    if (te.includes('chunked')) {
      return await this._readChunkedBody();
    } else if (cl !== undefined) {
      const len = parseInt(cl, 10);
      if (isNaN(len) || len <= 0) return Buffer.alloc(0);
      await this._readAtLeast(len);
      return this._consume(len);
    } else {
      // Read until connection close (drain the stream)
      return await this._readUntilEnd();
    }
  }

  async _readChunkedBody() {
    const chunks = [];
    let totalLen = 0;

    while (true) {
      // Read chunk size line
      const lineEnd = await this._readUntil('\r\n');
      const sizeLine = this._consume(lineEnd).toString('utf8').trim();
      this._consume(2); // consume \r\n

      const chunkSize = parseInt(sizeLine, 16);
      if (isNaN(chunkSize) || chunkSize === 0) {
        // Terminal chunk — consume trailing \r\n
        try { await this._readAtLeast(2); this._consume(2); } catch(e) {}
        break;
      }

      // Read chunk data + trailing \r\n
      await this._readAtLeast(chunkSize + 2);
      const chunkData = this._consume(chunkSize);
      this._consume(2); // trailing \r\n

      totalLen += chunkData.length;
      if (totalLen <= TrafficEntry.MAX_BODY_CAPTURE) {
        chunks.push(chunkData);
      }
    }

    return Buffer.concat(chunks);
  }

  async _readUntilEnd() {
    return new Promise((resolve) => {
      const chunks = [this.buffer];
      this.buffer = Buffer.alloc(0);

      const onData = (chunk) => chunks.push(chunk);
      const done = () => {
        this.stream.removeListener('data', onData);
        this.stream.removeListener('end', done);
        this.stream.removeListener('close', done);
        this.stream.removeListener('error', done);
        resolve(Buffer.concat(chunks));
      };
      this.stream.on('data', onData);
      this.stream.on('end', done);
      this.stream.on('close', done);
      this.stream.on('error', done);
    });
  }

  // Parse an HTTP request from the stream
  async readRequest() {
    // Read headers
    const headerEnd = await this._readUntil('\r\n\r\n');
    const headerBuf = this._consume(headerEnd);
    this._consume(4); // \r\n\r\n

    const headerStr = headerBuf.toString('utf8');
    const lines = headerStr.split('\r\n');
    const firstLine = lines[0];
    const parts = firstLine.split(' ');
    const method = parts[0];
    const path = parts[1];
    const httpVersion = parts[2] ? parts[2].replace('HTTP/', '') : '1.1';

    const headers = this._parseHeaders(lines.slice(1).join('\r\n'));
    const body = await this._readBody(headers);

    return { method, path, httpVersion, headers, body };
  }

  // Parse an HTTP response from the stream
  async readResponse() {
    const headerEnd = await this._readUntil('\r\n\r\n');
    const headerBuf = this._consume(headerEnd);
    this._consume(4);

    const headerStr = headerBuf.toString('utf8');
    const lines = headerStr.split('\r\n');
    const firstLine = lines[0];
    const statusParts = firstLine.split(' ');
    const httpVersion = statusParts[0] ? statusParts[0].replace('HTTP/', '') : '1.1';
    const statusCode = parseInt(statusParts[1], 10) || 0;
    const statusMessage = statusParts.slice(2).join(' ');

    const headers = this._parseHeaders(lines.slice(1).join('\r\n'));
    const body = await this._readResponseBody(headers, statusCode);

    return { httpVersion, statusCode, statusMessage, headers, body };
  }
}

module.exports = TLSHandler;
