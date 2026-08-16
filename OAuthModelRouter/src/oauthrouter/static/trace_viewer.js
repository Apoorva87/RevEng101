(function (global) {
  const JSON_DEFAULT_DEPTH = 1;
  const JSON_EXPANDED_DEPTH = 20;
  let treeIdCounter = 0;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"]/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]
    ));
  }

  function statusClass(status) {
    const numeric = Number(status);
    if (!Number.isFinite(numeric)) return '';
    if (numeric < 300) return 'status-2xx';
    if (numeric < 500) return 'status-4xx';
    return 'status-5xx';
  }

  function providerName(detail) {
    const path = detail?.incoming?.path || '';
    return path.split('/')[1] || '';
  }

  function providerBadge(detail) {
    const provider = providerName(detail);
    if (!provider) return '';
    return `<span class="provider-icon ${provider}"></span><span class="badge badge-${provider}">${provider}</span>`;
  }

  function finalAttempt(detail) {
    const attempts = detail?.attempts || [];
    return attempts.length ? attempts[attempts.length - 1] : null;
  }

  function formatHttpTarget(req) {
    if (!req || !Object.keys(req).length) return '(no request data)';
    const method = req.method || '?';
    const target = req.url || req.path || '';
    return `${method} ${target}`.trim();
  }

  function countLabel(noun, value) {
    const count = Object.keys(value || {}).length;
    return `${count} ${noun}${count === 1 ? '' : 's'}`;
  }

  function hasVisibleBody(body) {
    return Boolean(body && !body.is_empty && String(body.text || '').length);
  }

  function findHeaderValue(headers, name) {
    const wanted = String(name || '').toLowerCase();
    for (const [key, value] of Object.entries(headers || {})) {
      if (key.toLowerCase() === wanted) return String(value || '');
    }
    return '';
  }

  function isJsonContentType(contentType) {
    const normalized = String(contentType || '').toLowerCase();
    return normalized.includes('/json') || normalized.includes('+json');
  }

  function isEventStreamContentType(contentType) {
    return String(contentType || '').toLowerCase().includes('text/event-stream');
  }

  function tryParseJson(text) {
    const raw = String(text || '');
    if (!raw.trim()) return null;
    try {
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function parseStructuredPayload(body, headers = {}) {
    if (!body || body.is_empty) return null;
    if ((body.encoding || 'utf-8').toLowerCase() !== 'utf-8') return null;

    let candidate = String(body.text || '').trim();
    if (!candidate) return null;

    const eventStream = parseEventStream(candidate, headers);
    if (eventStream !== null) return eventStream;

    for (let depth = 0; depth < 3; depth += 1) {
      const parsed = tryParseJson(candidate);
      if (parsed === null) return null;
      if (parsed && typeof parsed === 'object') return parsed;
      if (typeof parsed === 'string') {
        const nested = parsed.trim();
        if (!nested || nested === candidate) return null;
        candidate = nested;
        continue;
      }
      return isJsonContentType(findHeaderValue(headers, 'content-type')) ? parsed : null;
    }

    return null;
  }

  function parseEventStream(text, headers = {}) {
    const raw = String(text || '');
    const normalized = raw.replace(/\r\n/g, '\n');
    const looksLikeEventStream = (
      isEventStreamContentType(findHeaderValue(headers, 'content-type')) ||
      normalized.startsWith('event:') ||
      normalized.startsWith('data:')
    );
    if (!looksLikeEventStream) return null;

    const chunks = normalized
      .split(/\n\n+/)
      .map(part => part.trim())
      .filter(Boolean);
    if (!chunks.length) return null;

    const events = [];
    for (const chunk of chunks) {
      const event = { lines: chunk.split('\n') };
      const dataLines = [];
      for (const line of event.lines) {
        if (line.startsWith('event:')) {
          event.event = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart());
        } else if (line.startsWith('id:')) {
          event.id = line.slice(3).trim();
        } else if (line.startsWith('retry:')) {
          event.retry = line.slice(6).trim();
        }
      }

      if (dataLines.length) {
        const joined = dataLines.join('\n');
        const parsed = tryParseJson(joined);
        event.data = parsed !== null ? parsed : joined;
      }

      delete event.lines;
      events.push(event);
    }

    return events.length ? { event_stream: events } : null;
  }

  function getBodyPreviewText(body) {
    if (!body || body.is_empty) return '';
    const raw = String(body.text || '');
    if ((body.encoding || 'utf-8').toLowerCase() !== 'utf-8') return raw;

    let decoded = raw;
    for (let depth = 0; depth < 2; depth += 1) {
      const parsed = tryParseJson(decoded);
      if (typeof parsed !== 'string') break;
      decoded = parsed;
    }
    return decoded;
  }

  function bodyMetaParts(body, headers = {}) {
    const meta = [];
    const contentType = findHeaderValue(headers, 'content-type');
    if (contentType) meta.push(contentType.split(';')[0]);
    if (body?.size_bytes) meta.push(`${body.size_bytes.toLocaleString()} bytes`);
    if ((body?.encoding || 'utf-8').toLowerCase() !== 'utf-8') meta.push(body.encoding);
    if (body?.text_truncated) {
      const shownChars = String(body.text || '').length;
      const totalChars = body.text_total_chars || shownChars;
      meta.push(`truncated ${shownChars.toLocaleString()} / ${totalChars.toLocaleString()} chars`);
    }
    return meta;
  }

  function nextTreeId() {
    treeIdCounter += 1;
    return `trace-tree-${treeIdCounter}`;
  }

  function mountJsonTree(element, value, openDepth) {
    if (!element) return;
    element.replaceChildren();
    if (typeof JSONFormatter === 'undefined') {
      const pre = document.createElement('pre');
      pre.className = 'json-tree-fallback';
      pre.textContent = JSON.stringify(value, null, 2);
      element.appendChild(pre);
      return;
    }
    const formatter = new JSONFormatter(value, openDepth, {
      theme: 'dark',
      hoverPreviewEnabled: true,
      hoverPreviewArrayCount: 50,
    });
    element.appendChild(formatter.render());
  }

  function renderJsonTrees(root, treeSpecs, openDepth) {
    root.__traceViewerTrees = treeSpecs;
    root.__traceViewerJsonDepth = openDepth;
    for (const spec of treeSpecs) {
      const element = root.querySelector(`#${spec.id}`);
      if (!element) continue;
      mountJsonTree(element, spec.value, openDepth);
    }
  }

  function remountJsonTrees(root, openDepth) {
    const treeSpecs = root.__traceViewerTrees || [];
    root.__traceViewerJsonDepth = openDepth;
    for (const spec of treeSpecs) {
      const element = root.querySelector(`#${spec.id}`);
      if (!element) continue;
      mountJsonTree(element, spec.value, openDepth);
    }
  }

  function setAllSectionsOpen(root, open) {
    root.querySelectorAll('details.trace-block').forEach(element => {
      element.open = open;
    });
  }

  function expandAll(root) {
    setAllSectionsOpen(root, true);
    remountJsonTrees(root, JSON_EXPANDED_DEPTH);
  }

  function collapseAll(root) {
    setAllSectionsOpen(root, false);
    remountJsonTrees(root, JSON_DEFAULT_DEPTH);
  }

  function renderSection(treeSpecs, title, contentHtml, { open = false, meta = [] } = {}) {
    const metaHtml = meta.length
      ? `<span class="trace-meta">${esc(meta.join(' · '))}</span>`
      : '';
    return `
      <details class="trace-block"${open ? ' open' : ''}>
        <summary class="trace-title">
          <span>${esc(title)}</span>
          ${metaHtml}
        </summary>
        ${contentHtml}
      </details>`;
  }

  function renderJsonSection(
    treeSpecs,
    title,
    value,
    { open = false, meta = [], emptyLabel = '(none)', treatEmptyObjectAsEmpty = false } = {}
  ) {
    const isEmptyObject = (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      !Object.keys(value).length
    );
    if (value === undefined || value === null || (treatEmptyObjectAsEmpty && isEmptyObject)) {
      return renderSection(
        treeSpecs,
        title,
        `<pre class="trace-pre trace-empty">${esc(emptyLabel)}</pre>`,
        { open, meta }
      );
    }

    const id = nextTreeId();
    treeSpecs.push({ id, value });
    return renderSection(
      treeSpecs,
      title,
      `<div class="json-tree" id="${id}"></div>`,
      { open, meta }
    );
  }

  function renderTextSection(
    treeSpecs,
    title,
    text,
    { open = false, meta = [], emptyLabel = '(none)' } = {}
  ) {
    const content = text && String(text).length ? String(text) : emptyLabel;
    const emptyClass = content === emptyLabel ? ' trace-empty' : '';
    return renderSection(
      treeSpecs,
      title,
      `<pre class="trace-pre${emptyClass}">${esc(content)}</pre>`,
      { open, meta }
    );
  }

  function renderBodySection(treeSpecs, title, body, headers, { open = true } = {}) {
    const meta = bodyMetaParts(body || {}, headers);
    const structured = parseStructuredPayload(body, headers);
    if (structured !== null) {
      return renderJsonSection(treeSpecs, title, structured, {
        open,
        meta,
        emptyLabel: '(empty body)',
      });
    }
    return renderTextSection(treeSpecs, title, getBodyPreviewText(body), {
      open,
      meta,
      emptyLabel: '(empty body)',
    });
  }

  function renderStageCard({ kicker, title, meta = [], sections = [] }) {
    return `
      <section class="trace-stage">
        <div class="trace-stage-head">
          <div>
            <div class="trace-stage-kicker">${esc(kicker)}</div>
            <div class="trace-stage-title">${esc(title)}</div>
          </div>
          ${meta.length
            ? `<div class="trace-stage-meta-list">${meta.map(item => `<span class="trace-pill">${esc(item)}</span>`).join('')}</div>`
            : ''}
        </div>
        <div class="trace-stage-body">
          ${sections.join('')}
        </div>
      </section>`;
  }

  function renderEmptyStage({ kicker, title, message }) {
    return renderStageCard({
      kicker,
      title,
      sections: [`<div class="trace-empty-note">${esc(message)}</div>`],
    });
  }

  function fullPageSummary(detail) {
    const attempt = finalAttempt(detail);
    const tokenUsed = attempt?.token_id || '—';
    const final = detail?.final || {};
    return `
      <div class="detail-header trace-page-summary">
        <div>
          <h3>
            ${providerBadge(detail)}
            <span class="${statusClass(final.status)}" style="margin-left:8px;font-family:var(--font)">${final.status || 'pending'}</span>
            <span style="color:var(--text2);font-weight:400"> · ${final.elapsed_ms ?? '—'}ms · token: <span style="color:var(--text)">${esc(tokenUsed)}</span> · ${(detail?.attempts || []).length} attempt${(detail?.attempts || []).length === 1 ? '' : 's'}</span>
          </h3>
          <div class="detail-warning">${esc(detail?.warning || 'Trace may contain authorization headers.')}</div>
        </div>
        <div class="trace-page-id mono">Log ${esc(detail?.id || '')}</div>
      </div>`;
  }

  function renderAttempt(treeSpecs, attempt, index) {
    const request = attempt?.request || {};
    const response = attempt?.response || {};
    return `
      <section class="trace-attempt-shell">
        <div class="trace-attempt-head">
          <div class="trace-attempt-label">Attempt ${index + 1}</div>
          <div class="trace-attempt-subtle">Token: ${esc(attempt?.token_id || 'unknown')}</div>
        </div>
        <div class="trace-attempt-grid">
          ${renderStageCard({
            kicker: 'Outgoing Request',
            title: formatHttpTarget(request),
            meta: [
              attempt?.token_id ? `token ${attempt.token_id}` : null,
              countLabel('header', request.headers),
            ].filter(Boolean),
            sections: [
              renderJsonSection(treeSpecs, 'Headers', request.headers || {}, {
                open: false,
                meta: [countLabel('header', request.headers)],
                emptyLabel: '(no headers)',
                treatEmptyObjectAsEmpty: true,
              }),
              renderBodySection(treeSpecs, 'Body', request.body, request.headers, {
                open: hasVisibleBody(request.body),
              }),
            ],
          })}
          ${renderStageCard({
            kicker: 'Incoming Response',
            title: `Status ${response.status ?? 'pending'}${response.streaming ? ' · streaming' : ''}`,
            meta: [
              countLabel('header', response.headers),
              response.streaming ? 'stream' : null,
            ].filter(Boolean),
            sections: [
              renderJsonSection(treeSpecs, 'Headers', response.headers || {}, {
                open: false,
                meta: [countLabel('header', response.headers)],
                emptyLabel: '(no headers)',
                treatEmptyObjectAsEmpty: true,
              }),
              renderBodySection(treeSpecs, 'Body', response.body, response.headers, {
                open: hasVisibleBody(response.body),
              }),
            ],
          })}
        </div>
      </section>`;
  }

  function makeDiffRow(key, leftValue, rightValue) {
    const left = leftValue == null ? null : String(leftValue);
    const right = rightValue == null ? null : String(rightValue);
    let kind = 'same';
    if (left === null && right !== null) kind = 'added';
    else if (left !== null && right === null) kind = 'removed';
    else if (left !== right) kind = 'changed';
    return { key, left, right, kind };
  }

  function requestDiffRows(incoming, outgoing) {
    const rows = [
      makeDiffRow(':target', formatHttpTarget(incoming), formatHttpTarget(outgoing)),
    ];
    const leftHeaders = incoming?.headers || {};
    const rightHeaders = outgoing?.headers || {};
    const keys = Array.from(new Set([
      ...Object.keys(leftHeaders),
      ...Object.keys(rightHeaders),
    ])).sort((a, b) => a.localeCompare(b));

    for (const key of keys) {
      rows.push(makeDiffRow(key, leftHeaders[key], rightHeaders[key]));
    }
    return rows;
  }

  function diffCounts(rows) {
    const counts = { changed: 0, added: 0, removed: 0 };
    for (const row of rows) {
      if (row.kind === 'same') continue;
      counts[row.kind] += 1;
    }
    return counts;
  }

  function diffValueHtml(row, side) {
    const value = side === 'left' ? row.left : row.right;
    const hasValue = value !== null && value !== undefined && value !== '';
    let className = 'trace-diff-value';
    if (row.kind === 'changed') {
      className += side === 'left' ? ' diff-removed' : ' diff-added';
    } else if (row.kind === 'removed') {
      className += side === 'left' ? ' diff-removed' : ' trace-diff-empty';
    } else if (row.kind === 'added') {
      className += side === 'right' ? ' diff-added' : ' trace-diff-empty';
    }

    return `
      <div class="${className}">
        ${hasValue ? esc(value) : '<span class="trace-diff-empty-mark">—</span>'}
      </div>`;
  }

  function renderDiffStage({ kicker, title, meta = [], rows = [], side }) {
    return `
      <section class="trace-stage">
        <div class="trace-stage-head">
          <div>
            <div class="trace-stage-kicker">${esc(kicker)}</div>
            <div class="trace-stage-title">${esc(title)}</div>
          </div>
          ${meta.length
            ? `<div class="trace-stage-meta-list">${meta.map(item => `<span class="trace-pill">${esc(item)}</span>`).join('')}</div>`
            : ''}
        </div>
        <div class="trace-stage-body">
          <div class="trace-diff-list">
            ${rows.map(row => `
              <div class="trace-diff-row">
                <div class="trace-diff-key">${esc(row.key)}</div>
                ${diffValueHtml(row, side)}
              </div>`).join('')}
          </div>
        </div>
      </section>`;
  }

  function previewSummary(detail, counts, detailsHref) {
    const attempt = finalAttempt(detail);
    const attempts = detail?.attempts || [];
    const final = detail?.final || {};
    const countBits = [];
    if (counts.changed) countBits.push(`${counts.changed} changed`);
    if (counts.added) countBits.push(`${counts.added} added`);
    if (counts.removed) countBits.push(`${counts.removed} removed`);
    const rewriteSummary = countBits.length ? countBits.join(' · ') : 'No request rewrites';
    const attemptLabel = attempts.length > 1
      ? `Showing final attempt of ${attempts.length}`
      : `${attempts.length || 0} attempt${attempts.length === 1 ? '' : 's'}`;

    return `
      <div class="detail-header">
        <div>
          <h3>
            ${providerBadge(detail)}
            <span class="${statusClass(final.status)}" style="margin-left:8px;font-family:var(--font)">${final.status || 'pending'}</span>
            <span style="color:var(--text2);font-weight:400"> · ${final.elapsed_ms ?? '—'}ms · token: <span style="color:var(--text)">${esc(attempt?.token_id || '—')}</span></span>
          </h3>
          <div class="detail-warning">${esc(rewriteSummary)} · ${esc(attemptLabel)}. Full bodies and responses open on the detail page.</div>
        </div>
        <a class="btn btn-sm" href="${detailsHref}" target="_blank" rel="noopener noreferrer">Details</a>
      </div>`;
  }

  function renderPreview(root, detail, options = {}) {
    const detailsHref = options.detailsHref || '#';
    const attempts = detail?.attempts || [];
    const incoming = detail?.incoming || {};
    const outgoing = attempts.length ? attempts[attempts.length - 1].request || {} : {};
    const rows = requestDiffRows(incoming, outgoing);
    const counts = diffCounts(rows);

    root.innerHTML = `
      ${previewSummary(detail, counts, detailsHref)}
      <div class="detail-grid">
        <div class="log-preview-grid">
          ${renderDiffStage({
            kicker: 'Incoming Request',
            title: formatHttpTarget(incoming),
            meta: [countLabel('header', incoming.headers)],
            rows,
            side: 'left',
          })}
          ${attempts.length
            ? renderDiffStage({
                kicker: 'Outgoing Request',
                title: formatHttpTarget(outgoing),
                meta: [
                  countLabel('header', outgoing.headers),
                  attempts.length > 1 ? 'final attempt' : null,
                ].filter(Boolean),
                rows,
                side: 'right',
              })
            : renderEmptyStage({
                kicker: 'Outgoing Request',
                title: 'No outgoing request captured',
                message: 'The router did not record an upstream attempt for this log entry.',
              })}
        </div>
      </div>`;
  }

  function renderFullPage(root, detail, options = {}) {
    const treeSpecs = [];
    const incoming = detail?.incoming || {};
    const attempts = detail?.attempts || [];

    root.innerHTML = `
      <div class="trace-page-shell">
        ${fullPageSummary(detail)}
        <div class="detail-grid">
          ${renderStageCard({
            kicker: 'Incoming Request',
            title: formatHttpTarget(incoming),
            meta: [countLabel('header', incoming.headers)],
            sections: [
              renderJsonSection(treeSpecs, 'Headers', incoming.headers || {}, {
                open: false,
                meta: [countLabel('header', incoming.headers)],
                emptyLabel: '(no headers)',
                treatEmptyObjectAsEmpty: true,
              }),
              renderBodySection(treeSpecs, 'Body', incoming.body, incoming.headers, {
                open: hasVisibleBody(incoming.body),
              }),
            ],
          })}
          ${attempts.length
            ? attempts.map((attempt, index) => renderAttempt(treeSpecs, attempt, index)).join('')
            : renderEmptyStage({
                kicker: 'Trace',
                title: 'No upstream attempts captured',
                message: 'The router recorded the incoming request, but no outgoing request or response was captured.',
              })}
        </div>
      </div>`;

    root.dataset.trace = JSON.stringify(detail, null, 2);
    renderJsonTrees(root, treeSpecs, options.jsonOpenDepth || JSON_DEFAULT_DEPTH);
  }

  global.TraceViewer = {
    renderPreview,
    renderFullPage,
    expandAll,
    collapseAll,
  };
})(window);
