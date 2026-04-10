const crypto = require('crypto');

let sequenceCounter = 0;

const MAX_BODY_CAPTURE = 2 * 1024 * 1024; // 2MB

const STATES = {
  PENDING: 'pending',
  INTERCEPTED: 'intercepted',
  FORWARDED: 'forwarded',
  COMPLETED: 'completed',
  ERROR: 'error',
  ABORTED: 'aborted',
};

class TrafficEntry {
  constructor({ method, url, httpVersion, headers, clientIp, clientPort, protocol, host, port }) {
    this.id = crypto.randomUUID();
    this.seq = ++sequenceCounter;
    this.state = STATES.PENDING;

    // Client info
    this.clientIp = clientIp || '';
    this.clientPort = clientPort || 0;

    // Request
    this.request = {
      method: method || 'GET',
      url: url || '/',
      httpVersion: httpVersion || '1.1',
      headers: headers || {},
      body: null,
      bodyTruncated: false,
      contentType: (headers && headers['content-type']) || '',
    };

    // Target
    this.target = {
      host: host || '',
      port: port || 80,
      protocol: protocol || 'http',
    };

    // Response (filled later)
    this.response = {
      statusCode: 0,
      statusMessage: '',
      httpVersion: '', // e.g. '2', '1.1'
      headers: {},
      body: null,
      bodyTruncated: false,
      contentType: '',
    };

    // Timing
    this.timing = {
      start: Date.now(),
      ttfb: 0,
      end: 0,
      duration: 0,
    };

    // Intercept metadata
    this.intercept = {
      wasIntercepted: false,
      wasModified: false,
      matchedRuleId: null,
      phase: null, // 'request' or 'response' — which is currently intercepted
    };
  }

  setRequestBody(buf) {
    if (!Buffer.isBuffer(buf)) buf = Buffer.from(buf);
    if (buf.length > MAX_BODY_CAPTURE) {
      this.request.body = buf.slice(0, MAX_BODY_CAPTURE);
      this.request.bodyTruncated = true;
    } else {
      this.request.body = buf;
    }
  }

  setResponse({ statusCode, statusMessage, headers, httpVersion }) {
    this.response.statusCode = statusCode;
    this.response.statusMessage = statusMessage || '';
    this.response.httpVersion = httpVersion || '';
    this.response.headers = headers || {};
    this.response.contentType = (headers && (headers['content-type'] || '')) || '';
    this.timing.ttfb = Date.now() - this.timing.start;
  }

  setResponseBody(buf) {
    if (!Buffer.isBuffer(buf)) buf = Buffer.from(buf);
    if (buf.length > MAX_BODY_CAPTURE) {
      this.response.body = buf.slice(0, MAX_BODY_CAPTURE);
      this.response.bodyTruncated = true;
    } else {
      this.response.body = buf;
    }
  }

  complete() {
    this.state = STATES.COMPLETED;
    this.timing.end = Date.now();
    this.timing.duration = this.timing.end - this.timing.start;
  }

  fail(error) {
    this.state = STATES.ERROR;
    this.error = error;
    this.timing.end = Date.now();
    this.timing.duration = this.timing.end - this.timing.start;
  }

  abort() {
    this.state = STATES.ABORTED;
    this.timing.end = Date.now();
    this.timing.duration = this.timing.end - this.timing.start;
  }

  toJSON() {
    return {
      id: this.id,
      seq: this.seq,
      state: this.state,
      clientIp: this.clientIp,
      clientPort: this.clientPort,
      request: {
        method: this.request.method,
        url: this.request.url,
        httpVersion: this.request.httpVersion,
        headers: this.request.headers,
        body: this.request.body ? this.request.body.toString('base64') : null,
        bodyTruncated: this.request.bodyTruncated,
        contentType: this.request.contentType,
        bodySize: this.request.body ? this.request.body.length : 0,
      },
      target: this.target,
      response: {
        statusCode: this.response.statusCode,
        statusMessage: this.response.statusMessage,
        httpVersion: this.response.httpVersion,
        headers: this.response.headers,
        body: this.response.body ? this.response.body.toString('base64') : null,
        bodyTruncated: this.response.bodyTruncated,
        contentType: this.response.contentType,
        bodySize: this.response.body ? this.response.body.length : 0,
      },
      timing: this.timing,
      intercept: this.intercept,
      error: this.error || null,
    };
  }

  // Lightweight version for list view (no bodies)
  toSummary() {
    return {
      id: this.id,
      seq: this.seq,
      state: this.state,
      method: this.request.method,
      url: this.request.url,
      statusCode: this.response.statusCode,
      contentType: this.response.contentType || this.request.contentType,
      host: this.target.host,
      duration: this.timing.duration,
      requestBodySize: this.request.body ? this.request.body.length : 0,
      responseBodySize: this.response.body ? this.response.body.length : 0,
      responseHttpVersion: this.response.httpVersion,
      intercept: this.intercept,
    };
  }
}

TrafficEntry.STATES = STATES;
TrafficEntry.MAX_BODY_CAPTURE = MAX_BODY_CAPTURE;

module.exports = TrafficEntry;
