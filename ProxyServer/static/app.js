(function() {
  'use strict';

  // ===== State =====
  const state = {
    entries: [],           // summary objects
    selectedId: null,
    selectedDetail: null,  // full detail object
    interceptEnabled: false,
    interceptQueue: 0,
    ws: null,
    connected: false,
    filters: { method: '', status: '', url: '', contentType: '' },
  };

  // ===== DOM refs =====
  const $ = (sel) => document.querySelector(sel);
  const tbody = $('#traffic-tbody');
  const detailEmpty = $('#detail-empty');
  const detailContent = $('#detail-content');
  const statusConnection = $('#status-connection');
  const statusCount = $('#status-count');
  const statusIntercept = $('#status-intercept');

  // ===== WebSocket =====
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.ws = new WebSocket(`${proto}//${location.host}`);

    state.ws.onopen = () => {
      state.connected = true;
      statusConnection.textContent = 'Connected';
      statusConnection.className = 'connected';
    };

    state.ws.onclose = () => {
      state.connected = false;
      statusConnection.textContent = 'Disconnected';
      statusConnection.className = 'disconnected';
      setTimeout(connectWS, 2000);
    };

    state.ws.onerror = () => {};

    state.ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      switch (msg.type) {
        case 'init':
          loadInitialTraffic();
          break;
        case 'add':
          addEntry(msg.entry);
          break;
        case 'update':
          updateEntry(msg.entry);
          break;
        case 'clear':
          state.entries = [];
          state.selectedId = null;
          state.selectedDetail = null;
          renderTrafficList();
          showEmptyDetail();
          break;
      }
    };
  }

  async function loadInitialTraffic() {
    try {
      const res = await fetch('/api/traffic');
      state.entries = await res.json();
      renderTrafficList();
      updateStatusBar();
    } catch(e) {
      console.error('Failed to load traffic:', e);
    }
  }

  function addEntry(entry) {
    state.entries.push(entry);
    if (passesFilter(entry)) {
      appendRow(entry);
    }
    updateStatusBar();
    // Auto-show intercept modal if entry is intercepted
    if (entry.state === 'intercepted' && state.interceptEnabled) {
      showInterceptModal(entry);
    }
  }

  function updateEntry(entry) {
    const idx = state.entries.findIndex(e => e.id === entry.id);
    if (idx >= 0) {
      state.entries[idx] = entry;
      const row = tbody.querySelector(`tr[data-id="${entry.id}"]`);
      if (row) {
        updateRow(row, entry);
      }
    }
    if (state.selectedId === entry.id && state.selectedDetail) {
      // Refresh detail if it's currently visible
      loadDetail(entry.id);
    }
    updateStatusBar();
  }

  // ===== Traffic List =====
  function renderTrafficList() {
    tbody.innerHTML = '';
    const filtered = state.entries.filter(passesFilter);
    for (const entry of filtered) {
      appendRow(entry);
    }
  }

  function appendRow(entry) {
    const tr = document.createElement('tr');
    tr.dataset.id = entry.id;
    updateRow(tr, entry);
    tr.addEventListener('click', () => selectEntry(entry.id));
    tbody.appendChild(tr);
    // Auto-scroll to bottom if near bottom
    const list = $('#traffic-list');
    if (list.scrollHeight - list.scrollTop - list.clientHeight < 100) {
      list.scrollTop = list.scrollHeight;
    }
  }

  function updateRow(tr, entry) {
    const url = entry.url || '';
    let parsedPath = url;
    try {
      const u = new URL(url);
      parsedPath = u.pathname + u.search;
    } catch(e) {}

    const statusClass = entry.statusCode >= 500 ? 'status-5xx'
      : entry.statusCode >= 400 ? 'status-4xx'
      : entry.statusCode >= 300 ? 'status-3xx'
      : entry.statusCode >= 200 ? 'status-2xx' : '';

    const ct = shortContentType(entry.contentType);
    const size = formatSize(entry.responseBodySize || 0);
    const time = entry.duration ? entry.duration + 'ms' : '-';
    const h2Badge = entry.responseHttpVersion === '2' ? '<span class="h2-badge">h2</span>' : '';

    tr.className = '';
    if (entry.id === state.selectedId) tr.classList.add('selected');
    if (entry.state === 'intercepted') tr.classList.add('intercepted');
    if (entry.state === 'error') tr.classList.add('error');

    tr.innerHTML = `
      <td class="col-seq">${entry.seq}</td>
      <td class="col-method"><span class="method-badge">${entry.method}</span></td>
      <td class="col-status"><span class="${statusClass}">${entry.statusCode || '-'}</span>${h2Badge}</td>
      <td class="col-host">${entry.host || ''}</td>
      <td class="col-path" title="${escapeHtml(url)}">${escapeHtml(parsedPath)}</td>
      <td class="col-type">${ct}</td>
      <td class="col-size">${size}</td>
      <td class="col-time">${time}</td>
    `;
  }

  // ===== Detail Panel =====
  async function selectEntry(id) {
    state.selectedId = id;
    // Update selection in list
    tbody.querySelectorAll('tr').forEach(tr => {
      tr.classList.toggle('selected', tr.dataset.id === id);
    });
    // Notify chat panel of selected entry
    if (window._chatSetSelectedEntry) window._chatSetSelectedEntry(id);
    await loadDetail(id);
  }

  async function loadDetail(id) {
    try {
      const res = await fetch(`/api/traffic/${id}`);
      if (!res.ok) return;
      state.selectedDetail = await res.json();
      renderDetail(state.selectedDetail);
    } catch(e) {
      console.error('Failed to load detail:', e);
    }
  }

  function showEmptyDetail() {
    detailEmpty.classList.remove('hidden');
    detailContent.classList.add('hidden');
  }

  function renderDetail(detail) {
    detailEmpty.classList.add('hidden');
    detailContent.classList.remove('hidden');

    // Request general
    $('#req-general').innerHTML = kvRows([
      ['Method', detail.request.method],
      ['URL', detail.request.url],
      ['HTTP Version', detail.request.httpVersion],
      ['Client', `${detail.clientIp}:${detail.clientPort}`],
      ['Target', `${detail.target.protocol}://${detail.target.host}:${detail.target.port}`],
    ]);

    // Request headers
    $('#req-headers').innerHTML = kvRows(
      Object.entries(detail.request.headers)
    );

    // Request body
    renderBody($('#req-body'), detail.request.body, detail.request.contentType, detail.request.bodySize);

    // Response general
    const resVersion = detail.response.httpVersion ? `HTTP/${detail.response.httpVersion}` : '';
    $('#res-general').innerHTML = kvRows([
      ['Status', `${detail.response.statusCode} ${detail.response.statusMessage}`],
      ['Protocol', resVersion || 'HTTP/1.1'],
      ['Content-Type', detail.response.contentType || '-'],
      ['Duration', detail.timing.duration ? detail.timing.duration + 'ms' : '-'],
      ['TTFB', detail.timing.ttfb ? detail.timing.ttfb + 'ms' : '-'],
    ]);

    // Response headers
    $('#res-headers').innerHTML = kvRows(
      Object.entries(detail.response.headers)
    );

    // Response body
    renderBody($('#res-body'), detail.response.body, detail.response.contentType, detail.response.bodySize);
  }

  function renderBody(container, base64Body, contentType, bodySize) {
    if (!base64Body || bodySize === 0) {
      container.innerHTML = '<span class="empty-body">No body</span>';
      return;
    }

    const raw = atob(base64Body);
    const ct = (contentType || '').toLowerCase();

    if (ct.includes('application/json') || ct.includes('+json')) {
      try {
        const parsed = JSON.parse(raw);
        container.innerHTML = renderJSON(parsed);
        initJSONToggles(container);
        return;
      } catch(e) {}
    }

    if (ct.includes('image/')) {
      container.innerHTML = `<img src="data:${contentType};base64,${base64Body}" alt="Image preview">`;
      return;
    }

    if (ct.includes('text/html') || ct.includes('text/css') || ct.includes('javascript')) {
      container.innerHTML = `<pre>${highlightSyntax(escapeHtml(raw), ct)}</pre>`;
      return;
    }

    // Check if it's text-like
    if (ct.includes('text/') || ct.includes('xml') || ct.includes('json')) {
      container.innerHTML = `<pre>${escapeHtml(raw)}</pre>`;
      return;
    }

    // Binary: hex dump
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    container.innerHTML = renderHexDump(bytes);
  }

  // ===== JSON Renderer =====
  function renderJSON(obj, indent = 0) {
    if (obj === null) return '<span class="json-null">null</span>';
    if (typeof obj === 'boolean') return `<span class="json-bool">${obj}</span>`;
    if (typeof obj === 'number') return `<span class="json-number">${obj}</span>`;
    if (typeof obj === 'string') return `<span class="json-string">"${escapeHtml(obj)}"</span>`;

    const pad = '  '.repeat(indent);
    const pad1 = '  '.repeat(indent + 1);

    if (Array.isArray(obj)) {
      if (obj.length === 0) return '<span class="json-bracket">[]</span>';
      const items = obj.map(v => pad1 + renderJSON(v, indent + 1)).join(',\n');
      const id = 'j' + Math.random().toString(36).slice(2, 8);
      return `<span class="json-bracket">[</span><span class="json-toggle" data-target="${id}"> ▼</span>\n<span id="${id}">${items}\n${pad}</span><span class="json-bracket">]</span>`;
    }

    const keys = Object.keys(obj);
    if (keys.length === 0) return '<span class="json-bracket">{}</span>';
    const items = keys.map(k => {
      return pad1 + `<span class="json-key">"${escapeHtml(k)}"</span>: ${renderJSON(obj[k], indent + 1)}`;
    }).join(',\n');
    const id = 'j' + Math.random().toString(36).slice(2, 8);
    return `<span class="json-bracket">{</span><span class="json-toggle" data-target="${id}"> ▼</span>\n<span id="${id}">${items}\n${pad}</span><span class="json-bracket">}</span>`;
  }

  function initJSONToggles(container) {
    container.querySelectorAll('.json-toggle').forEach(toggle => {
      toggle.addEventListener('click', () => {
        const target = container.querySelector('#' + toggle.dataset.target);
        if (target) {
          target.classList.toggle('json-collapsed');
          toggle.textContent = target.classList.contains('json-collapsed') ? ' ▶' : ' ▼';
        }
      });
    });
  }

  // ===== Hex Dump =====
  function renderHexDump(bytes, maxBytes = 2048) {
    const lines = [];
    const len = Math.min(bytes.length, maxBytes);
    for (let off = 0; off < len; off += 16) {
      const hexParts = [];
      const asciiParts = [];
      for (let i = 0; i < 16; i++) {
        if (off + i < len) {
          const b = bytes[off + i];
          hexParts.push(`<span class="hex-byte">${b.toString(16).padStart(2, '0')}</span>`);
          asciiParts.push(b >= 32 && b < 127
            ? `<span class="hex-ascii">${escapeHtml(String.fromCharCode(b))}</span>`
            : `<span class="hex-nonprint">.</span>`);
        } else {
          hexParts.push('  ');
          asciiParts.push(' ');
        }
      }
      lines.push(
        `<span class="hex-offset">${off.toString(16).padStart(8, '0')}</span>  ${hexParts.join(' ')}  ${asciiParts.join('')}`
      );
    }
    if (bytes.length > maxBytes) {
      lines.push(`<span class="hex-offset">... ${bytes.length - maxBytes} more bytes</span>`);
    }
    return '<pre class="hex-dump">' + lines.join('\n') + '</pre>';
  }

  // ===== Syntax Highlighting (lightweight) =====
  function highlightSyntax(html, ct) {
    if (ct.includes('javascript')) {
      return html
        .replace(/\b(const|let|var|function|return|if|else|for|while|class|import|export|from|new|this|async|await|try|catch|throw)\b/g, '<span class="syntax-keyword">$1</span>')
        .replace(/(["'`])(?:(?!\1).)*?\1/g, '<span class="syntax-string">$&</span>')
        .replace(/\/\/.*$/gm, '<span class="syntax-comment">$&</span>')
        .replace(/\b(\d+)\b/g, '<span class="syntax-number">$1</span>');
    }
    if (ct.includes('html') || ct.includes('xml')) {
      return html
        .replace(/(&lt;\/?)([\w-]+)/g, '$1<span class="syntax-tag">$2</span>')
        .replace(/([\w-]+)(=)/g, '<span class="syntax-attr">$1</span>$2')
        .replace(/(["'])(?:(?!\1).)*?\1/g, '<span class="syntax-string">$&</span>');
    }
    if (ct.includes('css')) {
      return html
        .replace(/([\w-]+)\s*:/g, '<span class="syntax-attr">$1</span>:')
        .replace(/(#[\da-fA-F]{3,8})\b/g, '<span class="syntax-number">$1</span>')
        .replace(/(["'])(?:(?!\1).)*?\1/g, '<span class="syntax-string">$&</span>')
        .replace(/\/\*[\s\S]*?\*\//g, '<span class="syntax-comment">$&</span>');
    }
    return html;
  }

  // ===== Filtering =====
  function passesFilter(entry) {
    const f = state.filters;
    if (f.method && entry.method !== f.method) return false;
    if (f.status) {
      const s = entry.statusCode;
      if (f.status === '2xx' && (s < 200 || s >= 300)) return false;
      if (f.status === '3xx' && (s < 300 || s >= 400)) return false;
      if (f.status === '4xx' && (s < 400 || s >= 500)) return false;
      if (f.status === '5xx' && (s < 500 || s >= 600)) return false;
    }
    if (f.url && !(entry.url || '').toLowerCase().includes(f.url.toLowerCase())) return false;
    if (f.contentType) {
      const ct = (entry.contentType || '').toLowerCase();
      if (!ct.includes(f.contentType)) return false;
    }
    return true;
  }

  // ===== Helpers =====
  function kvRows(pairs) {
    return pairs.map(([k, v]) =>
      `<div class="kv-row"><span class="kv-key">${escapeHtml(String(k))}</span><span class="kv-value">${escapeHtml(String(v || ''))}</span><button class="copy-btn" onclick="navigator.clipboard.writeText('${escapeHtml(String(v || '').replace(/'/g, "\\'"))}')">copy</button></div>`
    ).join('');
  }

  function escapeHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function shortContentType(ct) {
    if (!ct) return '';
    if (ct.includes('json')) return 'JSON';
    if (ct.includes('html')) return 'HTML';
    if (ct.includes('javascript')) return 'JS';
    if (ct.includes('css')) return 'CSS';
    if (ct.includes('image/')) return 'IMG';
    if (ct.includes('xml')) return 'XML';
    if (ct.includes('text/plain')) return 'TXT';
    if (ct.includes('form')) return 'Form';
    return ct.split('/').pop().split(';')[0].slice(0, 8);
  }

  function formatSize(bytes) {
    if (!bytes) return '-';
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'K';
    return (bytes / (1024 * 1024)).toFixed(1) + 'M';
  }

  function updateStatusBar() {
    statusCount.textContent = `${state.entries.length} requests`;
    const iq = state.entries.filter(e => e.state === 'intercepted').length;
    state.interceptQueue = iq;
    statusIntercept.textContent = `Intercept queue: ${iq}`;
  }

  // ===== Event Handlers =====

  // Tabs
  document.querySelectorAll('#detail-tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#detail-tabs .tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('hidden'));
      tab.classList.add('active');
      $(`#tab-${tab.dataset.tab}`).classList.remove('hidden');
    });
  });

  // Collapsible sections
  document.querySelectorAll('.collapsible-header').forEach(header => {
    header.addEventListener('click', () => {
      header.closest('.collapsible').classList.toggle('collapsed');
    });
  });

  // Filters
  let filterTimeout;
  function onFilterChange() {
    clearTimeout(filterTimeout);
    filterTimeout = setTimeout(() => {
      state.filters.method = $('#filter-method').value;
      state.filters.status = $('#filter-status').value;
      state.filters.url = $('#filter-url').value;
      state.filters.contentType = $('#filter-content-type').value;
      renderTrafficList();
    }, 150);
  }
  $('#filter-method').addEventListener('change', onFilterChange);
  $('#filter-status').addEventListener('change', onFilterChange);
  $('#filter-url').addEventListener('input', onFilterChange);
  $('#filter-content-type').addEventListener('change', onFilterChange);

  // Clear button
  $('#btn-clear').addEventListener('click', async () => {
    await fetch('/api/traffic', { method: 'DELETE' });
  });

  // Intercept toggle
  $('#btn-intercept').addEventListener('click', async () => {
    state.interceptEnabled = !state.interceptEnabled;
    const btn = $('#btn-intercept');
    btn.dataset.active = state.interceptEnabled;
    btn.textContent = `Intercept: ${state.interceptEnabled ? 'ON' : 'OFF'}`;
    try {
      await fetch('/api/intercept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: state.interceptEnabled }),
      });
    } catch(e) {}
  });

  // Rules modal
  $('#btn-rules').addEventListener('click', async () => {
    await loadRules();
    $('#rules-modal').classList.remove('hidden');
  });

  async function loadRules() {
    try {
      const res = await fetch('/api/rules');
      const rules = await res.json();
      const list = $('#rules-list');
      list.innerHTML = rules.map(r => `
        <div class="rule-item" data-id="${r.id}">
          <input type="checkbox" ${r.enabled ? 'checked' : ''} class="rule-toggle">
          <span class="rule-pattern">${escapeHtml(r.method || '*')} ${escapeHtml(r.urlPattern || '*')} [${r.direction}]</span>
          <button class="rule-delete">&times;</button>
        </div>
      `).join('') || '<p style="color:var(--text-dim)">No rules defined</p>';

      list.querySelectorAll('.rule-delete').forEach(btn => {
        btn.addEventListener('click', async () => {
          const id = btn.closest('.rule-item').dataset.id;
          await fetch(`/api/rules/${id}`, { method: 'DELETE' });
          loadRules();
        });
      });

      list.querySelectorAll('.rule-toggle').forEach(chk => {
        chk.addEventListener('change', async () => {
          const id = chk.closest('.rule-item').dataset.id;
          await fetch(`/api/rules/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: chk.checked }),
          });
        });
      });
    } catch(e) {}
  }

  $('#btn-add-rule').addEventListener('click', async () => {
    const pattern = prompt('URL pattern (glob, e.g. *api*):');
    if (!pattern) return;
    const method = prompt('Method filter (* for all):', '*') || '*';
    const direction = prompt('Direction (request/response/both):', 'request') || 'request';
    await fetch('/api/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urlPattern: pattern, method, direction }),
    });
    loadRules();
  });
  document.querySelectorAll('.modal-close, .modal-backdrop').forEach(el => {
    el.addEventListener('click', (e) => {
      e.target.closest('.modal').classList.add('hidden');
    });
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    // Don't handle if typing in input/textarea
    if (e.target.matches('input, textarea, select')) return;

    if (e.key === 'j' || e.key === 'k') {
      const filtered = state.entries.filter(passesFilter);
      if (filtered.length === 0) return;
      const currentIdx = filtered.findIndex(e2 => e2.id === state.selectedId);
      let newIdx;
      if (e.key === 'j') {
        newIdx = currentIdx < filtered.length - 1 ? currentIdx + 1 : currentIdx;
      } else {
        newIdx = currentIdx > 0 ? currentIdx - 1 : 0;
      }
      selectEntry(filtered[newIdx].id);
    }
    if (e.key === 'f') {
      e.preventDefault();
      $('#filter-url').focus();
    }
    if (e.key === 'i') {
      $('#btn-intercept').click();
    }
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal').forEach(m => m.classList.add('hidden'));
    }
  });

  // Save session
  $('#btn-save').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/sessions', { method: 'POST' });
      const data = await res.json();
      if (data.file) alert('Session saved: ' + data.file);
    } catch(e) {
      alert('Save not available yet');
    }
  });

  // Load session
  $('#btn-load').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/sessions');
      const sessions = await res.json();
      if (!sessions.length) { alert('No saved sessions'); return; }
      const name = prompt('Sessions:\n' + sessions.map((s,i) => `${i+1}. ${s}`).join('\n') + '\n\nEnter session number:');
      if (name) {
        const idx = parseInt(name, 10) - 1;
        if (sessions[idx]) {
          await fetch(`/api/sessions/${sessions[idx]}/load`, { method: 'POST' });
          loadInitialTraffic();
        }
      }
    } catch(e) {
      alert('Load not available yet');
    }
  });

  // Export HAR
  $('#btn-export-har').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/export/har');
      if (!res.ok) { alert('Export not available yet'); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `capture-${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.har`;
      a.click();
      URL.revokeObjectURL(url);
    } catch(e) {
      alert('Export not available yet');
    }
  });

  // ===== Intercept Modal =====
  let currentInterceptId = null;
  let currentInterceptPhase = null; // 'request' or 'response'

  function showInterceptModal(entry) {
    currentInterceptId = entry.id;
    currentInterceptPhase = (entry.intercept && entry.intercept.phase) || 'request';
    // Load full detail to populate fields
    fetch(`/api/traffic/${entry.id}`).then(r => r.json()).then(detail => {
      const modal = $('#intercept-modal');
      const header = modal.querySelector('.modal-header h2');

      if (currentInterceptPhase === 'response') {
        header.textContent = 'Intercepted Response';
        $('#intercept-method').value = detail.response.statusCode || '';
        $('#intercept-method').previousElementSibling.textContent = 'Status Code';
        $('#intercept-url').value = detail.request.url;
        $('#intercept-url').disabled = true;
        $('#intercept-url').previousElementSibling.textContent = 'Original URL (read-only)';
        $('#intercept-headers').value = JSON.stringify(detail.response.headers, null, 2);
        if (detail.response.body) {
          try { $('#intercept-body').value = atob(detail.response.body); }
          catch(e) { $('#intercept-body').value = ''; }
        } else {
          $('#intercept-body').value = '';
        }
      } else {
        header.textContent = 'Intercepted Request';
        $('#intercept-method').value = detail.request.method;
        $('#intercept-method').previousElementSibling.textContent = 'Method';
        $('#intercept-url').value = detail.request.url;
        $('#intercept-url').disabled = false;
        $('#intercept-url').previousElementSibling.textContent = 'URL';
        $('#intercept-headers').value = JSON.stringify(detail.request.headers, null, 2);
        if (detail.request.body) {
          try { $('#intercept-body').value = atob(detail.request.body); }
          catch(e) { $('#intercept-body').value = ''; }
        } else {
          $('#intercept-body').value = '';
        }
      }
      modal.classList.remove('hidden');
    }).catch(() => {});
  }

  $('#btn-forward').addEventListener('click', async () => {
    if (!currentInterceptId) return;
    const isResponse = currentInterceptPhase === 'response';
    const endpoint = isResponse ? 'forward-response' : 'forward';
    const modifications = {};

    if (isResponse) {
      const statusCode = parseInt($('#intercept-method').value.trim(), 10);
      if (statusCode) modifications.statusCode = statusCode;
      const headersText = $('#intercept-headers').value.trim();
      try { modifications.headers = JSON.parse(headersText); } catch(e) {}
      const bodyText = $('#intercept-body').value;
      if (bodyText) modifications.body = bodyText;
    } else {
      const method = $('#intercept-method').value.trim();
      const url = $('#intercept-url').value.trim();
      const headersText = $('#intercept-headers').value.trim();
      const bodyText = $('#intercept-body').value;
      if (method) modifications.method = method;
      if (url) { try { modifications.path = new URL(url).pathname + new URL(url).search; } catch(e) {} }
      try { modifications.headers = JSON.parse(headersText); } catch(e) {}
      if (bodyText) modifications.body = bodyText;
    }

    await fetch(`/api/traffic/${currentInterceptId}/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(modifications),
    });
    $('#intercept-modal').classList.add('hidden');
    $('#intercept-url').disabled = false;
    currentInterceptId = null;
    currentInterceptPhase = null;
  });

  $('#btn-drop').addEventListener('click', async () => {
    if (!currentInterceptId) return;
    const isResponse = currentInterceptPhase === 'response';
    const endpoint = isResponse ? 'drop-response' : 'drop';
    await fetch(`/api/traffic/${currentInterceptId}/${endpoint}`, { method: 'POST' });
    $('#intercept-modal').classList.add('hidden');
    $('#intercept-url').disabled = false;
    currentInterceptId = null;
    currentInterceptPhase = null;
  });

  // ===== Splitter Drag Logic =====
  (function initSplitters() {
    const hSplitter = document.getElementById('h-splitter');
    const vSplitter = document.getElementById('v-splitter');
    const trafficList = document.getElementById('traffic-list');
    const chatContainer = document.getElementById('chat-container');
    const mainEl = document.getElementById('main');

    // Restore saved sizes
    const savedTrafficWidth = localStorage.getItem('pane-traffic-width');
    if (savedTrafficWidth && trafficList) {
      trafficList.style.flexBasis = savedTrafficWidth + 'px';
    }
    const savedChatHeight = localStorage.getItem('pane-chat-height');
    if (savedChatHeight && chatContainer) {
      chatContainer.style.flexBasis = savedChatHeight + 'px';
    }

    // Horizontal splitter (traffic-list | detail-panel)
    if (hSplitter) {
      let dragging = false;
      hSplitter.addEventListener('mousedown', (e) => {
        e.preventDefault();
        dragging = true;
        hSplitter.classList.add('active');
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';

        const onMove = (e2) => {
          if (!dragging) return;
          const mainRect = mainEl.getBoundingClientRect();
          let newWidth = e2.clientX - mainRect.left;
          newWidth = Math.max(200, Math.min(newWidth, mainRect.width - 200));
          trafficList.style.flexBasis = newWidth + 'px';
        };

        const onUp = () => {
          dragging = false;
          hSplitter.classList.remove('active');
          document.body.style.userSelect = '';
          document.body.style.cursor = '';
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          localStorage.setItem('pane-traffic-width', parseInt(trafficList.style.flexBasis, 10));
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    }

    // Vertical splitter (main area | chat)
    if (vSplitter) {
      let dragging = false;
      vSplitter.addEventListener('mousedown', (e) => {
        e.preventDefault();
        dragging = true;
        vSplitter.classList.add('active');
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'row-resize';

        const onMove = (e2) => {
          if (!dragging) return;
          const statusbar = document.getElementById('statusbar');
          const statusH = statusbar ? statusbar.offsetHeight : 0;
          const bodyH = document.body.clientHeight;
          let chatH = bodyH - e2.clientY - statusH;
          chatH = Math.max(80, Math.min(chatH, bodyH * 0.5));
          chatContainer.style.flexBasis = chatH + 'px';
        };

        const onUp = () => {
          dragging = false;
          vSplitter.classList.remove('active');
          document.body.style.userSelect = '';
          document.body.style.cursor = '';
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          localStorage.setItem('pane-chat-height', parseInt(chatContainer.style.flexBasis, 10));
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    }
  })();

  // ===== Init =====
  connectWS();
})();
