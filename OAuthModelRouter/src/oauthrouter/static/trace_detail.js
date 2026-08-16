const TRACE_API = '';

async function traceApi(path) {
  const response = await fetch(TRACE_API + path, {
    headers: { 'Content-Type': 'application/json' },
  });
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (e) {
    data = raw ? { raw } : {};
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function traceLogIdFromPath() {
  const trimmed = window.location.pathname.replace(/\/+$/, '');
  const parts = trimmed.split('/');
  return decodeURIComponent(parts[parts.length - 1] || '');
}

async function copyCurrentTrace() {
  const root = document.getElementById('trace-page-root');
  const text = root.dataset.trace || '';
  if (!text) return;
  await navigator.clipboard.writeText(text);
  const button = document.getElementById('trace-copy-json');
  const original = button.textContent;
  button.textContent = 'Copied';
  setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

async function loadTracePage() {
  const logId = traceLogIdFromPath();
  const root = document.getElementById('trace-page-root');
  const title = document.getElementById('trace-page-title');

  title.textContent = `Trace ${logId || ''}`;
  root.innerHTML = '<div class="empty-state">Loading trace...</div>';

  if (!logId) {
    root.innerHTML = '<div class="empty-state">No trace id was provided.</div>';
    return;
  }

  try {
    const detail = await traceApi(`/api/logs/${encodeURIComponent(logId)}`);
    TraceViewer.renderFullPage(root, detail);
    title.textContent = `Trace ${detail.id || logId}`;
  } catch (error) {
    root.innerHTML = `<div class="empty-state">${error.message || 'Could not load trace.'}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('trace-page-root');
  document.getElementById('trace-expand-all').addEventListener('click', () => {
    TraceViewer.expandAll(root);
  });
  document.getElementById('trace-collapse-all').addEventListener('click', () => {
    TraceViewer.collapseAll(root);
  });
  document.getElementById('trace-copy-json').addEventListener('click', () => {
    copyCurrentTrace().catch(() => {});
  });
  loadTracePage();
});
