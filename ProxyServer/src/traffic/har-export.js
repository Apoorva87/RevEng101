function harExport(entries) {
  return {
    log: {
      version: '1.2',
      creator: {
        name: 'ProxyServer',
        version: '1.0.0',
      },
      entries: entries.map(entry => {
        const req = entry.request || {};
        const res = entry.response || {};
        const timing = entry.timing || {};

        return {
          startedDateTime: new Date(timing.start || Date.now()).toISOString(),
          time: timing.duration || 0,
          request: {
            method: req.method || 'GET',
            url: req.url || '',
            httpVersion: 'HTTP/' + (req.httpVersion || '1.1'),
            cookies: [],
            headers: toHarHeaders(req.headers),
            queryString: parseQueryString(req.url),
            postData: req.body ? {
              mimeType: req.contentType || 'application/octet-stream',
              text: req.body.toString('utf8'),
            } : undefined,
            headersSize: -1,
            bodySize: req.body ? req.body.length : 0,
          },
          response: {
            status: res.statusCode || 0,
            statusText: res.statusMessage || '',
            httpVersion: 'HTTP/1.1',
            cookies: [],
            headers: toHarHeaders(res.headers),
            content: {
              size: res.body ? res.body.length : 0,
              mimeType: res.contentType || 'application/octet-stream',
              text: res.body ? res.body.toString('utf8') : '',
            },
            redirectURL: (res.headers && res.headers['location']) || '',
            headersSize: -1,
            bodySize: res.body ? res.body.length : 0,
          },
          cache: {},
          timings: {
            send: 0,
            wait: timing.ttfb || 0,
            receive: timing.duration ? timing.duration - (timing.ttfb || 0) : 0,
          },
          serverIPAddress: (entry.target && entry.target.host) || '',
          connection: String((entry.target && entry.target.port) || ''),
        };
      }),
    },
  };
}

function toHarHeaders(headers) {
  if (!headers) return [];
  return Object.entries(headers).map(([name, value]) => ({
    name,
    value: String(value),
  }));
}

function parseQueryString(urlStr) {
  if (!urlStr) return [];
  try {
    const u = new URL(urlStr);
    const params = [];
    u.searchParams.forEach((value, name) => {
      params.push({ name, value });
    });
    return params;
  } catch(e) {
    return [];
  }
}

module.exports = harExport;
