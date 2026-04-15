const API = '';
let providerCache = [];
let providerTestState = {};
let providerSelections = {};
let editingProviderName = null;
let refreshAllPending = false;

function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast toast-${type} show`;
  setTimeout(() => el.classList.remove('show'), 3000);
}

async function api(path, opts = {}) {
  const {
    suppressErrorToast = false,
    ...fetchOpts
  } = opts;
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...(fetchOpts.headers || {}) },
    ...fetchOpts,
  });
  const raw = await res.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (e) {
    data = raw ? { raw } : {};
  }
  if (!res.ok) {
    const message = data.error || `Request failed (${res.status})`;
    if (!suppressErrorToast) toast(message, 'error');
    const err = new Error(message);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// ─── Token list ──────────────────────────────────────────────
async function loadTokens() {
  try {
    const [tokens, health, providers] = await Promise.all([
      api('/api/tokens'),
      api('/health'),
      api('/api/providers'),
    ]);
    providerCache = providers;
    renderTokens(tokens);
    renderStats(tokens, health);
    renderProviders(providers);
    setEndpointPorts();
  } catch (e) {
    document.getElementById('providers-body').innerHTML =
      '<div class="empty-state">Could not load provider controls.</div>';
    document.getElementById('tokens-body').innerHTML =
      '<div class="empty-state">Could not load tokens.</div>';
  }
}

function renderStats(tokens, health) {
  const claude = tokens.filter(t => t.provider === 'claude');
  const openai = tokens.filter(t => t.provider === 'openai');
  const activeC = claude.filter(t => t.status === 'healthy' && !t.is_expired);
  const activeO = openai.filter(t => t.status === 'healthy' && !t.is_expired);

  document.getElementById('stat-total').textContent = tokens.length;
  document.getElementById('stat-total-sub').textContent =
    `${activeC.length + activeO.length} active, ${tokens.length - activeC.length - activeO.length} inactive`;

  document.getElementById('stat-claude').innerHTML =
    `<span class="${activeC.length > 0 ? 'green' : 'red'}">${activeC.length}</span><span style="color:var(--text2);font-size:16px"> / ${claude.length}</span>`;
  document.getElementById('stat-claude-sub').textContent = activeC.length > 0 ? 'active' : 'no active tokens';

  document.getElementById('stat-openai').innerHTML =
    `<span class="${activeO.length > 0 ? 'green' : 'red'}">${activeO.length}</span><span style="color:var(--text2);font-size:16px"> / ${openai.length}</span>`;
  document.getElementById('stat-openai-sub').textContent = activeO.length > 0 ? 'active' : 'no active tokens';

  const status = health.status;
  document.getElementById('stat-router').innerHTML =
    `<span class="${status === 'ok' ? 'green' : 'orange'}">${status.toUpperCase()}</span>`;
  document.getElementById('stat-router-sub').textContent =
    Object.entries(health.providers || {}).map(([k, v]) => `${k}: ${v.healthy_tokens}/${v.total_tokens}`).join(', ');
}

function renderProviders(providers) {
  const body = document.getElementById('providers-body');
  if (!providers.length) {
    body.innerHTML = '<div class="empty-state">No providers configured.</div>';
    return;
  }

  providers.forEach(provider => {
    if (providerSelections[provider.name] === undefined) {
      // Default-select the first healthy token so the user sees which token will be tested
      const firstHealthy = (provider.tokens || []).find(t => t.status === 'healthy' && !t.is_expired);
      providerSelections[provider.name] = firstHealthy ? firstHealthy.id : '';
    }
  });

  body.innerHTML = `
    <div class="provider-grid">
      ${providers.map(provider => renderProviderCard(provider)).join('')}
    </div>`;
}

function renderProviderCard(provider) {
  const state = providerTestState[provider.name];
  const providerId = providerDomId(provider.name);
  const cooldownText = provider.cooling_tokens?.length
    ? provider.cooling_tokens.map(t => `${t.id} until ${timeUntil(t.retry_at)}`).join(', ')
    : 'No provider cooldowns';
  const endpointHost = provider.upstream.replace(/^https?:\/\//, '');
  const authLabel = provider.auth_prefix
    ? `${provider.auth_header}: ${provider.auth_prefix} …`
    : provider.auth_header;
  const selectedToken = providerSelections[provider.name] || '';
  const tokenOptions = [
    `<option value="">Auto-select best token</option>`,
    ...(provider.tokens || []).map(token => {
      const statusBits = [];
      if (token.status !== 'healthy') statusBits.push(token.status);
      if (token.is_expired) statusBits.push('expired');
      const label = statusBits.length
        ? `${token.id} (${statusBits.join(', ')})`
        : `${token.id} (healthy)`;
      return `<option value="${esc(token.id)}" ${selectedToken === token.id ? 'selected' : ''}>${esc(label)}</option>`;
    }),
  ].join('');

  return `
    <div class="provider-card">
      <div class="provider-card-header">
        <div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <span class="provider-icon ${provider.name}"></span>
            <span class="badge badge-${provider.name}">${esc(provider.name)}</span>
          </div>
          <h3>${esc(capitalize(provider.name))}</h3>
          <div class="provider-subtitle">Provider defaults used by the router whenever token-specific settings are absent.</div>
        </div>
        <div class="provider-card-actions">
          <button class="btn btn-sm" onclick='openProviderModal(${jsStr(provider.name)})'>Edit</button>
        </div>
      </div>
      <div class="provider-inline-controls">
        <select id="provider-token-${providerId}" onchange='setProviderTokenSelection(${jsStr(provider.name)}, this.value)'>
          ${tokenOptions}
        </select>
        <button class="btn btn-sm btn-primary" ${state?.pending ? 'disabled' : ''} onclick='testProvider(${jsStr(provider.name)})'>
          ${state?.pending ? 'Testing...' : 'Test Provider'}
        </button>
      </div>
      <div class="provider-meta">
        <span class="meta-pill">${provider.active_tokens}/${provider.total_tokens} active tokens</span>
        <span class="meta-pill">${provider.healthy_tokens} healthy</span>
        <span class="meta-pill">${provider.extra_headers && Object.keys(provider.extra_headers).length ? Object.keys(provider.extra_headers).length + ' extra header' + (Object.keys(provider.extra_headers).length > 1 ? 's' : '') : 'No extra headers'}</span>
      </div>
      <div class="provider-card-body">
        <div class="provider-metric">
          <div class="provider-metric-label">Upstream</div>
          <div class="provider-metric-value mono">${esc(endpointHost)}</div>
        </div>
        <div class="provider-metric">
          <div class="provider-metric-label">Auth Scheme</div>
          <div class="provider-metric-value mono">${esc(authLabel)}</div>
        </div>
        <div class="provider-metric">
          <div class="provider-metric-label">Token Endpoint</div>
          <div class="provider-metric-value mono">${esc(provider.token_endpoint || 'Not set')}</div>
        </div>
        <div class="provider-metric">
          <div class="provider-metric-label">Cooldowns</div>
          <div class="provider-metric-value">${esc(cooldownText)}</div>
        </div>
      </div>
      <div class="provider-test-result" data-state="${providerTestVisualState(state)}">
        <div class="provider-test-title">Last provider test</div>
        <div class="provider-test-summary">${providerTestSummary(state)}</div>
        <div class="provider-test-meta">${providerTestMeta(state, provider)}</div>
      </div>
    </div>`;
}

function providerTestVisualState(state) {
  if (!state) return 'idle';
  if (state.pending) return 'pending';
  return state.ok ? 'success' : 'error';
}

function providerTestSummary(state) {
  if (!state) return 'No test has been run yet. Use the button above to verify live upstream routing.';
  if (state.batch && state.pending) {
    return `Running live checks for ${state.tested_tokens || 0} token connection${state.tested_tokens === 1 ? '' : 's'}...`;
  }
  if (state.batch) {
    const tested = state.tested_tokens || 0;
    if (!tested) return 'No token connections were tested for this provider.';
    return `Batch refresh tested ${tested} token connection${tested === 1 ? '' : 's'}: ${state.ok_tokens || 0} healthy, ${state.failed_tokens || 0} failed.`;
  }
  if (state.pending) return 'Running a live provider check with the current selected token...';
  if (state.ok) return `Healthy response from upstream (${state.status_code}) using token ${esc(state.token_id)}.`;
  return esc(state.error || `Provider test failed with status ${state.status_code || 'unknown'}.`);
}

function providerTestMeta(state, provider) {
  if (!state) return `<div class="provider-test-line">Endpoint: ${esc(provider.upstream)}</div>`;
  if (state.batch) {
    const lines = [];
    const headerParts = [];
    if (state.checked_at) headerParts.push(`Checked ${timeAgo(state.checked_at)}`);
    if (state.tested_tokens !== undefined) headerParts.push(`Connections: ${state.tested_tokens}`);
    if (headerParts.length) {
      lines.push(`<div class="provider-test-line">${esc(headerParts.join(' · '))}</div>`);
    }
    for (const result of (state.token_results || [])) {
      lines.push(providerBatchTokenLine(result));
    }
    return lines.join('') || `<div class="provider-test-line">Endpoint: ${esc(provider.upstream)}</div>`;
  }
  const lines = [];
  const summaryParts = [];
  if (state.test_url) summaryParts.push(`Test URL: ${esc(state.test_url)}`);
  if (state.latency_ms !== undefined) summaryParts.push(`Latency: ${state.latency_ms}ms`);
  if (state.attempts && state.attempts > 1) summaryParts.push(`Attempts: ${state.attempts}`);
  if (state.retry_at) summaryParts.push(`Retry at: ${esc(formatRateLimitReset(state.retry_at))}`);
  if (state.checked_at) summaryParts.push(`Checked ${timeAgo(state.checked_at)}`);
  if (summaryParts.length) lines.push(summaryParts.join(' · '));
  const limitSummary = rateLimitSummary(state.rate_limits);
  if (limitSummary) lines.push(`Limits: ${esc(limitSummary)}`);
  const resetSummary = rateLimitResetSummary(state.rate_limits);
  if (resetSummary) lines.push(`Resets: ${esc(resetSummary)}`);
  if (state.snippet) lines.push(`Reply: ${esc(state.snippet)}`);
  if (state.notes && state.notes.length) lines.push(esc(state.notes.join(' · ')));
  if (!lines.length) lines.push(`Endpoint: ${esc(provider.upstream)}`);
  return lines.map(line => `<div class="provider-test-line">${line}</div>`).join('');
}

async function testProvider(name) {
  await runProviderTest(name);
}

async function runProviderTest(name, options = {}) {
  const {
    reloadAfter = true,
    toastResult = true,
  } = options;
  const selectedTokenId = providerSelections[name] || '';
  providerTestState[name] = { pending: true };
  renderProviders(providerCache);
  try {
    const result = await api(`/api/providers/${encodeURIComponent(name)}/test`, {
      method: 'POST',
      body: JSON.stringify(selectedTokenId ? { token_id: selectedTokenId } : {}),
      suppressErrorToast: !toastResult,
    });
    providerTestState[name] = result;
    if (toastResult) toast(`${capitalize(name)} test succeeded`);
    if (reloadAfter) {
      await loadTokens();
    } else {
      renderProviders(providerCache);
    }
    return { ok: true, result };
  } catch (e) {
    providerTestState[name] = {
      ...(e.data || {}),
      ok: false,
      checked_at: new Date().toISOString(),
    };
    if (toastResult) toast(`${capitalize(name)} test failed`, 'error');
    if (reloadAfter) {
      await loadTokens();
    } else {
      renderProviders(providerCache);
    }
    return { ok: false, error: e };
  }
}

async function runTokenTest(tokenId, options = {}) {
  const { toastResult = false } = options;
  try {
    const result = await api(`/api/tokens/${encodeURIComponent(tokenId)}/test`, {
      method: 'POST',
      suppressErrorToast: !toastResult,
    });
    const normalized = {
      ...result,
      token_id: result.token_id || tokenId,
      status_code: result.status_code ?? result.status ?? 0,
    };
    if (toastResult) {
      toast(
        normalized.ok
          ? `Token "${normalized.token_id}" test succeeded`
          : `Token "${normalized.token_id}" test failed`,
        normalized.ok ? 'success' : 'error',
      );
    }
    return normalized;
  } catch (e) {
    const normalized = {
      ...(e.data || {}),
      ok: false,
      token_id: tokenId,
      checked_at: new Date().toISOString(),
      status_code: e.data?.status_code ?? e.data?.status ?? e.status ?? 0,
      error: e.data?.error || e.message || 'Token test failed',
    };
    if (toastResult) toast(`Token "${tokenId}" test failed`, 'error');
    return normalized;
  }
}

function setProviderTokenSelection(name, tokenId) {
  providerSelections[name] = tokenId || '';
}

function openProviderModal(name) {
  const provider = providerCache.find(p => p.name === name);
  if (!provider) return;
  editingProviderName = name;
  document.getElementById('p-name').value = provider.name;
  document.getElementById('p-upstream').value = provider.upstream || '';
  document.getElementById('p-auth-header').value = provider.auth_header || '';
  document.getElementById('p-auth-prefix').value = provider.auth_prefix || '';
  document.getElementById('p-token-endpoint').value = provider.token_endpoint || '';
  document.getElementById('p-oauth-client-id').value = provider.oauth_client_id || '';
  document.getElementById('p-extra-headers').value = provider.extra_headers && Object.keys(provider.extra_headers).length
    ? JSON.stringify(provider.extra_headers, null, 2)
    : '';
  document.getElementById('provider-modal').classList.add('active');
}

function closeProviderModal() {
  editingProviderName = null;
  document.getElementById('provider-modal').classList.remove('active');
}

async function saveProvider() {
  if (!editingProviderName) return;
  let extraHeaders = null;
  const rawHeaders = document.getElementById('p-extra-headers').value.trim();
  if (rawHeaders) {
    try {
      extraHeaders = JSON.parse(rawHeaders);
    } catch (e) {
      toast('Extra headers must be valid JSON', 'error');
      return;
    }
  }

  try {
    await api(`/api/providers/${encodeURIComponent(editingProviderName)}`, {
      method: 'PATCH',
      body: JSON.stringify({
        upstream: document.getElementById('p-upstream').value.trim(),
        auth_header: document.getElementById('p-auth-header').value.trim(),
        auth_prefix: document.getElementById('p-auth-prefix').value.trim(),
        token_endpoint: document.getElementById('p-token-endpoint').value.trim(),
        oauth_client_id: document.getElementById('p-oauth-client-id').value.trim(),
        extra_headers: extraHeaders,
      }),
    });
    toast(`Saved provider "${editingProviderName}"`);
    closeProviderModal();
    await loadTokens();
  } catch (e) {}
}

function renderTokens(tokens) {
  const body = document.getElementById('tokens-body');
  if (!tokens.length) {
    body.innerHTML = '<div class="empty-state">No tokens configured yet. Add one to the local token database.</div>';
    return;
  }
  body.innerHTML = `
    <table class="token-table">
      <thead>
        <tr>
          <th>Enabled</th>
          <th>Name</th>
          <th>Provider</th>
          <th>Status</th>
          <th>Rate Limit</th>
          <th>Resets<br><span style="font-weight:400;color:var(--text2)">(5h / 7d)</span></th>
          <th>Expires</th>
          <th>Token</th>
          <th>Priority</th>
          <th>Last Used</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${tokens.map(t => `
          <tr${t.status === 'unhealthy' && !t.is_expired ? ' style="opacity:0.5"' : ''}>
            <td>
              <label class="toggle" title="${t.status === 'healthy' ? 'Disable this token' : 'Enable this token'}">
                <input type="checkbox" ${t.status === 'healthy' ? 'checked' : ''}
                       onchange="toggleToken('${esc(t.id)}', this.checked)">
                <span class="toggle-slider"></span>
              </label>
            </td>
            <td style="font-weight:600">${esc(t.id)}</td>
            <td>
              <span class="provider-icon ${t.provider}"></span>
              <span class="badge badge-${t.provider}">${t.provider}</span>
            </td>
            <td>
              <span class="badge ${t.is_expired ? 'badge-expired' : 'badge-' + t.status}">
                ${t.is_expired ? 'EXPIRED' : t.status}
              </span>
            </td>
            <td>${renderRateLimits(t.rate_limits)}</td>
            <td>${renderRateLimitResets(t.rate_limits)}</td>
            <td class="mono">${esc(t.expires_in)}</td>
            <td class="mono">${esc(t.masked_token)}</td>
            <td>
              <input type="number" value="${Number(t.priority ?? 100)}" step="1"
                     onchange="updateTokenPriority('${esc(t.id)}', this.value)"
                     style="width:76px;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px">
            </td>
            <td class="mono">${t.last_used_at ? timeAgo(t.last_used_at) : '—'}</td>
            <td style="white-space:nowrap">
              <button class="btn btn-sm" onclick='openEditModal(${jsStr(t.id)})'>Edit</button>
              ${t.has_refresh_token ? `<button class="btn btn-sm" onclick="refreshToken('${esc(t.id)}')">Refresh</button>` : ''}
              <button class="btn btn-sm btn-danger" onclick="deleteToken('${esc(t.id)}')">Delete</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>`;
}

function renderRateLimits(rl) {
  if (!rl) return '<span class="mono" style="color:var(--text2)">—</span>';
  const windows = rateLimitWindows(rl);
  if (!windows.length) return '<span class="mono" style="color:var(--text2)">—</span>';
  return `<div class="rl-cell"><div class="rl-bars">${windows.map(w => rlBar(w.label, w.utilization, w.status, w.reset)).join('')}</div></div>`;
}

function renderRateLimitResets(rl) {
  const lines = rateLimitResetLines(rl);
  if (!lines.length) return '<span class="mono" style="color:var(--text2)">—</span>';
  return `<div class="rl-reset-cell">${lines.map(line => `<div class="rl-reset-line">${esc(line)}</div>`).join('')}</div>`;
}

function rateLimitWindows(rl) {
  if (!rl) return [];
  if (Array.isArray(rl.windows) && rl.windows.length) {
    return rl.windows.filter(w => w && w.utilization !== undefined && w.utilization !== null);
  }
  const legacy = [
    { label: '5h', utilization: rl['5h_utilization'], status: rl['5h_status'], reset: rl['5h_reset'] },
    { label: '5d', utilization: rl['5d_utilization'], status: rl['5d_status'], reset: rl['5d_reset'] },
    { label: '7d', utilization: rl['7d_utilization'], status: rl['7d_status'], reset: rl['7d_reset'] },
  ];
  return legacy.filter(w => w.utilization !== undefined && w.utilization !== null);
}

function rateLimitSummary(rl) {
  const windows = rateLimitWindows(rl);
  if (!windows.length) return '';
  return windows.map(w => `${w.label} ${rateLimitPercent(w.utilization)}%`).join(', ');
}

function rateLimitResetSummary(rl) {
  const windows = rateLimitWindows(rl).filter(w => w && w.reset);
  if (!windows.length) return '';
  return windows.map(w => `${w.label} ${formatRateLimitReset(w.reset)}`).join(', ');
}

function rateLimitResetLines(rl) {
  const windows = rateLimitWindows(rl);
  if (!windows.length) return [];
  const byLabel = new Map(windows.map(w => [String(w.label || ''), w]));
  const primary = byLabel.get('5h') || byLabel.get('5d');
  const secondary = byLabel.get('7d');
  if (!primary?.reset && !secondary?.reset) return [];
  return [
    primary?.reset ? formatRateLimitReset(primary.reset) : '—',
    secondary?.reset ? formatRateLimitReset(secondary.reset) : '—',
  ];
}

function rateLimitPercent(util) {
  const numeric = Number(util);
  if (!Number.isFinite(numeric) || numeric < 0) return 0;
  return Math.round(numeric * 100);
}

function providerConnections(provider) {
  return Array.isArray(provider?.tokens) ? provider.tokens : [];
}

function summarizeBatchProviderTests(results) {
  const tokenResults = (results || []).map(result => ({
    token_id: result.token_id,
    ok: !!result.ok,
    status_code: result.status_code ?? result.status ?? 0,
    latency_ms: result.latency_ms,
    checked_at: result.checked_at,
    rate_limits: result.rate_limits,
    snippet: result.snippet,
    error: result.error,
  }));
  const okTokens = tokenResults.filter(result => result.ok).length;
  return {
    batch: true,
    ok: tokenResults.length > 0 && okTokens === tokenResults.length,
    checked_at: new Date().toISOString(),
    tested_tokens: tokenResults.length,
    ok_tokens: okTokens,
    failed_tokens: tokenResults.length - okTokens,
    token_results: tokenResults,
  };
}

function providerBatchTokenLine(result) {
  const detailParts = [];
  const statusCode = result.status_code ?? result.status ?? 0;
  detailParts.push(`${result.ok ? 'ok' : 'failed'} (${statusCode || 'unknown'})`);
  if (result.latency_ms !== undefined) detailParts.push(`${result.latency_ms}ms`);
  const limitSummary = rateLimitSummary(result.rate_limits);
  if (limitSummary) detailParts.push(limitSummary);
  const resetSummary = rateLimitResetSummary(result.rate_limits);
  if (resetSummary) detailParts.push(`resets ${resetSummary}`);
  if (!result.ok) {
    const message = result.error || result.snippet;
    if (message) detailParts.push(message);
  }
  return `<div class="provider-test-line"><span class="mono">${esc(result.token_id || 'unknown')}</span>: ${esc(detailParts.join(' · '))}</div>`;
}

function formatRateLimitReset(value) {
  if (value === undefined || value === null || value === '') return 'unknown';
  const raw = String(value).trim();
  let date = null;
  if (/^\d+(\.\d+)?$/.test(raw)) {
    let numeric = Number(raw);
    if (Number.isFinite(numeric)) {
      if (numeric > 1_000_000_000_000) numeric /= 1000;
      date = new Date(numeric * 1000);
    }
  } else {
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) date = parsed;
  }
  if (!date || Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function rlBar(label, util, status, reset) {
  if (util === undefined || util === null) return '';
  const pct = rateLimitPercent(util);
  const barPct = Math.max(0, Math.min(pct, 100));
  const color = pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--orange)' : 'var(--green)';
  const pctColor = pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--orange)' : 'var(--text2)';
  const title = [status, reset ? `reset ${formatRateLimitReset(reset)}` : ''].filter(Boolean).join(' · ');
  return `<div class="rl-row">
    <span class="rl-label">${esc(label)}</span>
    <div class="rl-track" title="${esc(title)}"><div class="rl-fill" style="width:${barPct}%;background:${color}"></div></div>
    <span class="rl-pct" style="color:${pctColor}">${pct}%</span>
  </div>`;
}

async function deleteToken(id) {
  if (!confirm(`Delete token "${id}"? This cannot be undone.`)) return;
  await api(`/api/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' });
  toast(`Token "${id}" deleted`);
  loadTokens();
}

async function refreshToken(id) {
  toast(`Refreshing "${id}"...`);
  try {
    const res = await api(`/api/tokens/${encodeURIComponent(id)}/refresh`, { method: 'POST' });
    toast(`Token "${id}" refreshed successfully!`);
    loadTokens();
  } catch (e) {}
}

async function updateTokenPriority(id, value) {
  const priority = Number(value);
  if (!Number.isInteger(priority)) {
    toast('Priority must be an integer', 'error');
    loadTokens();
    return;
  }
  try {
    await api(`/api/tokens/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ priority }),
    });
    toast(`Priority saved for "${id}"`);
    loadTokens();
  } catch (e) {}
}

// ─── Add modals ──────────────────────────────────────────────
function openAddModal() {
  document.getElementById('add-modal').classList.add('active');
}
function closeAddModal() {
  document.getElementById('add-modal').classList.remove('active');
}

async function addManualToken() {
  const name = document.getElementById('m-name').value.trim();
  const provider = document.getElementById('m-provider').value;
  const at = document.getElementById('m-access-token').value.trim();
  const rt = document.getElementById('m-refresh-token').value.trim();
  const priority = Number(document.getElementById('m-priority').value || 100);
  if (!name || !at) { toast('Name and access token are required', 'error'); return; }

  try {
    await api('/api/tokens', {
      method: 'POST',
      body: JSON.stringify({
        id: name, provider, access_token: at,
        refresh_token: rt || null,
        priority,
      }),
    });
    toast(`Token "${name}" added`);
    closeAddModal();
    loadTokens();
  } catch (e) {}
}

// ─── Helpers ─────────────────────────────────────────────────
function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function jsStr(s) {
  return JSON.stringify(String(s ?? ''));
}

function providerDomId(name) {
  return String(name || '').replace(/[^a-zA-Z0-9_-]+/g, '-');
}

function timeAgo(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function timeUntil(iso) {
  if (!iso) return 'soon';
  const d = new Date(iso);
  const secs = Math.max(0, Math.floor((d.getTime() - Date.now()) / 1000));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

function capitalize(s) {
  if (!s) return '';
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function setEndpointPorts() {
  const port = window.location.port || '8000';
  document.querySelectorAll('#ep-port-claude, #ep-port-openai')
    .forEach(el => el.textContent = port);
  // Update hero commands with the actual port
  const claudeCmd = document.getElementById('hero-cmd-claude');
  const codexCmd = document.getElementById('hero-cmd-codex');
  if (claudeCmd) claudeCmd.textContent = `ANTHROPIC_BASE_URL=http://localhost:${port}/claude ANTHROPIC_API_KEY=oauthrouter claude --bare`;
  if (codexCmd) codexCmd.textContent = `OPENAI_BASE_URL=http://localhost:${port}/openai codex`;
}

async function copyHeroCmd(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.textContent);
    toast('Copied to clipboard');
  } catch (e) {
    toast('Copy failed', 'error');
  }
}

function setRefreshAllButtonState() {
  const button = document.getElementById('refresh-all-btn');
  if (!button) return;
  button.disabled = refreshAllPending;
  button.textContent = refreshAllPending ? 'Testing All...' : 'Refresh';
}

async function refreshAll() {
  if (refreshAllPending) return;
  const providersToTest = providerCache
    .map(provider => ({
      name: provider.name,
      tokens: providerConnections(provider),
    }))
    .filter(provider => provider.tokens.length);

  if (!providersToTest.length) {
    await loadTokens();
    if (document.getElementById('page-logs').classList.contains('active')) loadLogs();
    toast('No provider connections available to test', 'error');
    return;
  }

  refreshAllPending = true;
  setRefreshAllButtonState();
  try {
    const results = [];
    for (const provider of providersToTest) {
      providerTestState[provider.name] = {
        batch: true,
        pending: true,
        tested_tokens: provider.tokens.length,
      };
    }
    renderProviders(providerCache);

    for (const provider of providersToTest) {
      const providerResults = [];
      for (const token of provider.tokens) {
        const result = await runTokenTest(token.id, { toastResult: false });
        providerResults.push(result);
        results.push(result);
      }
      providerTestState[provider.name] = summarizeBatchProviderTests(providerResults);
      renderProviders(providerCache);
    }
    await loadTokens();
    if (document.getElementById('page-logs').classList.contains('active')) loadLogs();
    const failed = results.filter(r => !r.ok).length;
    const succeeded = results.length - failed;
    const testedLabel = `${results.length} connection${results.length === 1 ? '' : 's'}`;
    toast(
      failed
        ? `Tested ${testedLabel}: ${succeeded} ok, ${failed} failed`
        : `Tested ${testedLabel} successfully`
    , failed ? 'error' : 'success');
  } finally {
    refreshAllPending = false;
    setRefreshAllButtonState();
  }
}

// ─── Page tabs ──────────────────────────────────────────────
function switchPage(page) {
  document.querySelectorAll('.page-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page-content').forEach(t => t.classList.remove('active'));

  const pages = ['tokens', 'logs'];
  const idx = pages.indexOf(page);
  document.querySelectorAll('.page-tab')[idx].classList.add('active');
  document.getElementById(`page-${page}`).classList.add('active');

  if (page === 'logs') {
    loadLogs();
    startLogPolling();
    setEndpointPorts();
  } else {
    stopLogPolling();
  }
}

// ─── Logs ───────────────────────────────────────────────────
let logInterval = null;

async function loadLogs() {
  try {
    const logs = await api('/api/logs');
    renderLogs(logs);
  } catch(e) {}
}

function renderLogs(logs) {
  const body = document.getElementById('logs-body');
  if (!logs.length) {
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:40px">No requests logged yet. Send a request through the proxy to see it here.</td></tr>';
    return;
  }
  body.innerHTML = logs.map(l => {
    const statusClass = l.status < 300 ? 'status-2xx' : l.status < 500 ? 'status-4xx' : 'status-5xx';
    const ts = new Date(l.timestamp);
    const time = ts.toLocaleTimeString();
    return `
      <tr>
        <td style="color:var(--text2)">${time}</td>
        <td>${l.method}</td>
        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis" title="${esc(l.path)}">${esc(l.path)}</td>
        <td><span class="provider-icon ${l.provider}"></span><span class="badge badge-${l.provider}">${l.provider}</span></td>
        <td>${l.token_used ? esc(l.token_used) : '<span style="color:var(--text2)">—</span>'}</td>
        <td><span class="${statusClass}">${l.status}</span></td>
        <td>${l.elapsed_ms}ms</td>
        <td style="color:var(--text2)">${esc(l.client)}</td>
        <td><button class="btn btn-sm" onclick="loadLogDetail('${esc(l.id)}')">Details</button></td>
      </tr>`;
  }).join('');
}

async function loadLogDetail(id) {
  const panel = document.getElementById('log-detail');
  panel.innerHTML = '<div class="empty-state">Loading request details...</div>';
  try {
    const detail = await api(`/api/logs/${encodeURIComponent(id)}`);
    renderLogDetail(detail);
  } catch (e) {
    panel.innerHTML = '<div class="empty-state">Could not load request details.</div>';
  }
}

function renderLogDetail(detail) {
  const panel = document.getElementById('log-detail');
  const incoming = detail.incoming || {};
  const attempts = detail.attempts || [];
  const final = detail.final || {};
  const lastAttempt = attempts.length ? attempts[attempts.length - 1] : null;
  const provider = incoming.path ? incoming.path.split('/')[1] || '' : '';
  const providerBadge = provider
    ? `<span class="provider-icon ${provider}"></span><span class="badge badge-${provider}">${provider}</span>`
    : '';
  const tokenUsed = lastAttempt ? lastAttempt.token_id || 'unknown' : '—';
  const statusClass = final.status < 300 ? 'status-2xx' : final.status < 500 ? 'status-4xx' : 'status-5xx';
  panel.innerHTML = `
    <div class="detail-header">
      <div>
        <h3>
          ${providerBadge}
          <span class="${statusClass}" style="margin-left:8px;font-family:var(--font)">${final.status || 'pending'}</span>
          <span style="color:var(--text2);font-weight:400"> · ${final.elapsed_ms ?? '—'}ms · token: <span style="color:var(--text)">${esc(tokenUsed)}</span> · ${attempts.length} attempt${attempts.length === 1 ? '' : 's'}</span>
        </h3>
        <div class="detail-warning">${esc(detail.warning || 'Trace may contain authorization headers.')}</div>
      </div>
      <button class="btn btn-sm" onclick="copyTrace('${esc(detail.id || '')}')">Copy JSON</button>
    </div>
    ${attempts.map((a, i) => {
      const req = a.request || {};
      const res = a.response || {};
      return `
        ${attempts.length > 1 ? `<div style="padding:8px 14px;border-bottom:1px solid var(--border);font-size:12px;font-weight:600;color:var(--text2);background:var(--surface2)">Attempt ${i + 1} · Token: ${esc(a.token_id || 'unknown')}</div>` : ''}
        <div class="detail-grid">
          ${diffBlock('Request In', 'Request Out', formatRequestLines(incoming), formatRequestLines(req))}
          ${plainBlock('Response Headers', formatResponseHeaderLines(res))}
          ${plainBlock('Response Body', formatResponseBodyLines(res))}
        </div>`;
    }).join('')}
    ${!attempts.length ? `<div class="detail-grid">${traceBlock('Incoming Request', formatRequestLines(incoming).join('\\n'), 'full')}</div>` : ''}`;
  panel.dataset.trace = JSON.stringify(detail, null, 2);
}

function diffBlock(leftTitle, rightTitle, leftLines, rightLines) {
  const leftSet = new Set(leftLines);
  const rightSet = new Set(rightLines);
  const leftHtml = leftLines.map(l => {
    if (l === '' || l.startsWith('──')) return `<div class="diff-sep">${esc(l)}</div>`;
    const cls = !rightSet.has(l) ? ' diff-removed' : '';
    return `<div class="diff-line${cls}">${esc(l)}</div>`;
  }).join('');
  const rightHtml = rightLines.map(l => {
    if (l === '' || l.startsWith('──')) return `<div class="diff-sep">${esc(l)}</div>`;
    const cls = !leftSet.has(l) ? ' diff-added' : '';
    return `<div class="diff-line${cls}">${esc(l)}</div>`;
  }).join('');
  return `
    <div class="trace-block">
      <div class="trace-title">${esc(leftTitle)}</div>
      <div class="trace-pre diff-pre">${leftHtml}</div>
    </div>
    <div class="trace-block">
      <div class="trace-title">${esc(rightTitle)}</div>
      <div class="trace-pre diff-pre">${rightHtml}</div>
    </div>`;
}

function plainBlock(title, lines) {
  const html = lines.map(l => {
    if (l === '' || l.startsWith('──')) return `<div class="diff-sep">${esc(l)}</div>`;
    return `<div class="diff-line">${esc(l)}</div>`;
  }).join('');
  return `
    <div class="trace-block">
      <div class="trace-title">${esc(title)}</div>
      <div class="trace-pre diff-pre">${html}</div>
    </div>`;
}

function traceBlock(title, text, extra = '') {
  return `
    <div class="trace-block ${extra}">
      <div class="trace-title">${esc(title)}</div>
      <pre class="trace-pre">${esc(text)}</pre>
    </div>`;
}

function formatRequestLines(req) {
  if (!req || !Object.keys(req).length) return ['(no data)'];
  const lines = [`${req.method || '?'} ${req.url || req.path || ''}`];
  lines.push('── Headers ──────────────────────────');
  for (const [k, v] of Object.entries(req.headers || {})) lines.push(`${k}: ${v}`);
  if (!Object.keys(req.headers || {}).length) lines.push('(none)');
  lines.push('── Body ─────────────────────────────');
  lines.push(...formatBodyLines(req.body || {}));
  return lines;
}

function formatResponseHeaderLines(res) {
  if (!res || !Object.keys(res).length) return ['(no response)'];
  const lines = [`Status: ${res.status ?? '?'}${res.streaming ? ' (streaming)' : ''}`];
  lines.push('── Headers ──────────────────────────');
  for (const [k, v] of Object.entries(res.headers || {})) lines.push(`${k}: ${v}`);
  if (!Object.keys(res.headers || {}).length) lines.push('(none)');
  return lines;
}

function formatResponseBodyLines(res) {
  if (!res || !Object.keys(res).length) return ['(no response)'];
  return formatBodyLines(res.body || {});
}

function formatBodyLines(body) {
  if (!body || body.is_empty) return ['(empty)'];
  const text = body.text || '';
  if ((body.encoding || '').toLowerCase() !== 'utf-8') return [text];
  try {
    const pretty = JSON.stringify(JSON.parse(text), null, 2);
    return pretty.split('\n');
  } catch (e) {
    return text.split('\n');
  }
}

function formatHeaders(headers) {
  const entries = Object.entries(headers);
  if (!entries.length) return '(none)';
  return entries.map(([k, v]) => `${k}: ${v}`).join('\n');
}

async function copyTrace(id) {
  const panel = document.getElementById('log-detail');
  const text = panel.dataset.trace || '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied trace ${id}`);
  } catch (e) {
    toast('Copy failed', 'error');
  }
}

function startLogPolling() {
  stopLogPolling();
  logInterval = setInterval(() => {
    if (document.getElementById('log-auto-refresh')?.checked) loadLogs();
  }, 3000);
}

function stopLogPolling() {
  if (logInterval) { clearInterval(logInterval); logInterval = null; }
}

function clearLogsView() {
  document.getElementById('logs-body').innerHTML =
    '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:40px">Cleared. New requests will appear here.</td></tr>';
  document.getElementById('log-detail').innerHTML =
    '<div class="empty-state">Select a request to inspect headers and payloads.</div>';
}

// ─── Token enable/disable ───────────────────────────────────
async function toggleToken(id, enabled) {
  const action = enabled ? 'enable' : 'disable';
  try {
    await api(`/api/tokens/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    toast(`Token "${id}" ${enabled ? 'enabled' : 'disabled'}`);
    loadTokens();
  } catch (e) {}
}

// ─── Edit Token Modal ────────────────────────
let editingTokenId = null;
let editingTokenData = null;

async function openEditModal(tokenId) {
  // Fetch fresh token data
  try {
    const tokens = await api('/api/tokens');
    editingTokenData = tokens.find(t => t.id === tokenId);
    if (!editingTokenData) {
      toast(`Token "${tokenId}" not found`, 'error');
      return;
    }
  } catch (e) { return; }

  editingTokenId = tokenId;
  document.getElementById('e-name').value = editingTokenData.id;
  document.getElementById('e-provider').value = editingTokenData.provider;
  document.getElementById('e-access-token').value = '';
  document.getElementById('e-refresh-token').value = '';
  document.getElementById('e-priority').value = editingTokenData.priority ?? 100;
  document.getElementById('edit-modal').classList.add('active');
}

function closeEditModal() {
  editingTokenId = null;
  editingTokenData = null;
  document.getElementById('edit-modal').classList.remove('active');
}

async function saveEditToken() {
  if (!editingTokenId) return;
  const newName = document.getElementById('e-name').value.trim();
  const accessToken = document.getElementById('e-access-token').value.trim();
  const refreshToken = document.getElementById('e-refresh-token').value.trim();
  const priority = Number(document.getElementById('e-priority').value);

  if (!newName) { toast('Name is required', 'error'); return; }
  if (!Number.isInteger(priority)) { toast('Priority must be an integer', 'error'); return; }

  const patch = { priority };
  if (newName !== editingTokenId) patch.name = newName;
  if (accessToken) patch.access_token = accessToken;
  if (refreshToken) patch.refresh_token = refreshToken;

  try {
    const result = await api(`/api/tokens/${encodeURIComponent(editingTokenId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    const updated = result.updated || [];
    toast(`Token saved${updated.length ? ': ' + updated.join(', ') : ''}`);
    closeEditModal();
    // Clear provider selections cache so renamed tokens get re-selected
    providerSelections = {};
    loadTokens();
  } catch (e) {}
}

// ─── Init ────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', loadTokens);

// Close modal on overlay click
document.getElementById('add-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeAddModal();
});
document.getElementById('provider-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeProviderModal();
});
document.getElementById('edit-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeEditModal();
});

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeAddModal();
    closeProviderModal();
    closeEditModal();
  }
});
