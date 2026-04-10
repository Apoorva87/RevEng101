(function() {
  'use strict';

  // ===== State =====
  const state = {
    messages: [],       // {id, role: 'user'|'assistant'|'system'|'error', content}
    streaming: false,
    available: false,
    contextToggles: {
      includeTraffic: true,
      includeSelected: false,
      includeRules: false,
      includeSource: false,
      includeBrowser: false,
    },
    tokenEstimate: 0,
    breakdown: {},
    sessionName: null,
    ws: null,
    connected: false,
    currentStreamId: null,
    currentStreamText: '',
  };

  const container = document.getElementById('chat-container');
  if (!container) return;

  // ===== Build DOM =====
  function buildUI() {
    container.innerHTML = '';

    const panel = document.createElement('div');
    panel.className = 'chat-panel';

    // Header
    const header = document.createElement('div');
    header.className = 'chat-header';
    header.innerHTML = `
      <div class="chat-header-info">
        <span class="chat-header-title">AI Chat</span>
        <span class="chat-header-session" id="chat-session-name">${state.sessionName ? state.sessionName : ''}</span>
      </div>
      <div class="chat-header-actions"></div>
    `;
    panel.appendChild(header);

    // Context toggles
    const ctx = document.createElement('div');
    ctx.className = 'chat-context';

    const togglesDiv = document.createElement('div');
    togglesDiv.className = 'chat-context-toggles';

    const toggleDefs = [
      { key: 'includeTraffic', label: 'Traffic' },
      { key: 'includeSelected', label: 'Selected' },
      { key: 'includeRules', label: 'Rules' },
      { key: 'includeSource', label: 'Source' },
      { key: 'includeBrowser', label: 'Browser' },
    ];

    for (const t of toggleDefs) {
      const label = document.createElement('label');
      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.checked = state.contextToggles[t.key];
      chk.addEventListener('change', () => {
        state.contextToggles[t.key] = chk.checked;
        sendToggles();
      });
      label.appendChild(chk);
      label.appendChild(document.createTextNode(' ' + t.label));
      togglesDiv.appendChild(label);
    }
    ctx.appendChild(togglesDiv);

    // Token bar
    const tokenBar = document.createElement('div');
    tokenBar.className = 'chat-token-bar';
    tokenBar.id = 'chat-token-bar';
    ctx.appendChild(tokenBar);

    const tokenLabel = document.createElement('div');
    tokenLabel.className = 'chat-token-label';
    tokenLabel.id = 'chat-token-label';
    tokenLabel.textContent = `~${formatTokens(state.tokenEstimate)} tokens`;
    ctx.appendChild(tokenLabel);

    panel.appendChild(ctx);

    // Messages area
    const messagesDiv = document.createElement('div');
    messagesDiv.className = 'chat-messages';
    messagesDiv.id = 'chat-messages';

    if (state.messages.length === 0 && !state.available) {
      const sysMsg = document.createElement('div');
      sysMsg.className = 'chat-msg system';
      sysMsg.textContent = 'Claude Code CLI not found. Install it and ensure "claude" is in your PATH.';
      messagesDiv.appendChild(sysMsg);
    } else if (state.messages.length === 0) {
      const sysMsg = document.createElement('div');
      sysMsg.className = 'chat-msg system';
      sysMsg.textContent = 'Ask me about captured traffic, intercept rules, or the proxy source code. Try /help for commands.';
      messagesDiv.appendChild(sysMsg);
    }

    for (const msg of state.messages) {
      messagesDiv.appendChild(createMessageEl(msg));
    }

    // Streaming indicator
    if (state.streaming) {
      if (state.currentStreamText) {
        const streamEl = document.createElement('div');
        streamEl.className = 'chat-msg assistant';
        streamEl.id = 'chat-stream-msg';
        streamEl.innerHTML = renderMarkdown(state.currentStreamText);
        messagesDiv.appendChild(streamEl);
      }
      const typing = document.createElement('div');
      typing.className = 'chat-typing';
      typing.id = 'chat-typing';
      typing.innerHTML = '<div class="chat-typing-dot"></div><div class="chat-typing-dot"></div><div class="chat-typing-dot"></div>';
      messagesDiv.appendChild(typing);
    }

    panel.appendChild(messagesDiv);

    // Input area
    const inputArea = document.createElement('div');
    inputArea.className = 'chat-input-area';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'chat-input';
    input.id = 'chat-input';
    input.placeholder = 'Type a message...';
    input.disabled = state.streaming;

    const sendBtn = document.createElement('button');
    sendBtn.className = 'chat-send-btn';
    sendBtn.textContent = 'Send';
    sendBtn.disabled = state.streaming;

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });
    sendBtn.addEventListener('click', handleSend);

    inputArea.appendChild(input);
    inputArea.appendChild(sendBtn);
    panel.appendChild(inputArea);

    container.appendChild(panel);

    // Scroll to bottom
    messagesDiv.scrollTop = messagesDiv.scrollHeight;

    // Focus input
    input.focus();

    // Update token bar
    updateTokenBar();
  }

  // ===== Message rendering =====
  function createMessageEl(msg) {
    const el = document.createElement('div');
    el.className = `chat-msg ${msg.role}`;
    if (msg.role === 'assistant') {
      el.innerHTML = renderMarkdown(msg.content);
    } else {
      el.textContent = msg.content;
    }
    return el;
  }

  function renderMarkdown(text) {
    if (!text) return '';
    // Escape HTML first
    let html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Code blocks (```)
    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      return `<pre><code>${code.trim()}</code></pre>`;
    });

    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Italic
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

    // Line breaks
    html = html.replace(/\n/g, '<br>');

    return html;
  }

  // ===== Token bar =====
  function updateTokenBar() {
    const bar = document.getElementById('chat-token-bar');
    const label = document.getElementById('chat-token-label');
    if (!bar || !label) return;

    const b = state.breakdown || {};
    const total = state.tokenEstimate || 1;
    const maxTokens = 100000; // budget reference
    const displayTotal = Math.min(total, maxTokens);

    const segments = [
      { key: 'preamble', cls: 'preamble' },
      { key: 'traffic', cls: 'traffic' },
      { key: 'selected', cls: 'selected' },
      { key: 'rules', cls: 'rules' },
      { key: 'source', cls: 'source' },
      { key: 'browser', cls: 'browser' },
    ];

    bar.innerHTML = '';
    for (const seg of segments) {
      const val = b[seg.key] || 0;
      if (val <= 0) continue;
      const pct = (val / maxTokens) * 100;
      const el = document.createElement('div');
      el.className = `chat-token-segment ${seg.cls}`;
      el.style.width = Math.max(pct, 0.5) + '%';
      el.title = `${seg.key}: ~${formatTokens(val)}`;
      bar.appendChild(el);
    }

    label.textContent = `~${formatTokens(total)} tokens`;
  }

  function formatTokens(n) {
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

  // ===== Commands =====
  function handleSend() {
    const input = document.getElementById('chat-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    // Handle slash commands
    if (text === '/reset') {
      sendWS({ type: 'chat:reset', messageId: genId() });
      state.messages = [];
      state.sessionName = null;
      state.messages.push({ id: genId(), role: 'system', content: 'Conversation reset.' });
      input.value = '';
      buildUI();
      return;
    }
    if (text === '/compact') {
      sendWS({ type: 'chat:compact', messageId: genId() });
      state.messages.push({ id: genId(), role: 'system', content: 'Compacting conversation...' });
      input.value = '';
      buildUI();
      return;
    }
    if (text === '/help') {
      state.messages.push({ id: genId(), role: 'system', content:
        'Commands:\n/reset - Clear conversation\n/compact - Summarize & compress context\n/help - Show this help\n\nContext toggles control what the AI can see. Toggle them in the header area.' });
      input.value = '';
      buildUI();
      return;
    }

    // Normal message
    const msgId = genId();
    state.messages.push({ id: msgId, role: 'user', content: text });
    state.streaming = true;
    state.currentStreamId = msgId;
    state.currentStreamText = '';

    // Get selectedEntryId from app.js state via a data attribute on body
    const selectedEntryId = document.body.dataset.selectedEntryId || null;

    sendWS({
      type: 'chat:send',
      messageId: msgId,
      text,
      selectedEntryId,
      contextToggles: state.contextToggles,
    });

    input.value = '';
    buildUI();
  }

  function sendToggles() {
    sendWS({
      type: 'chat:context-toggle',
      toggles: state.contextToggles,
    });
  }

  // ===== WebSocket =====
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.ws = new WebSocket(`${proto}//${location.host}`);

    state.ws.onopen = () => {
      state.connected = true;
    };

    state.ws.onclose = () => {
      state.connected = false;
      setTimeout(connectWS, 3000);
    };

    state.ws.onerror = () => {};

    state.ws.onmessage = (evt) => {
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch (e) {
        return;
      }

      // Only handle chat: messages, ignore traffic messages (handled by app.js)
      if (!msg.type || !msg.type.startsWith('chat:')) return;

      switch (msg.type) {
        case 'chat:status':
          state.available = msg.available;
          state.tokenEstimate = msg.tokenEstimate || 0;
          state.breakdown = msg.breakdown || {};
          if (msg.sessionName) {
            state.sessionName = msg.sessionName;
            const sessionEl = document.getElementById('chat-session-name');
            if (sessionEl) sessionEl.textContent = msg.sessionName;
          }
          if (msg.contextToggles) {
            // Don't override user toggles on reconnect, just update availability
          }
          updateTokenBar();
          break;

        case 'chat:chunk':
          state.currentStreamText += msg.text || '';
          updateStreamMessage();
          break;

        case 'chat:action':
          updateActionIndicator(msg.detail || msg.action);
          break;

        case 'chat:done':
          state.streaming = false;
          state.messages.push({
            id: msg.messageId,
            role: 'assistant',
            content: msg.fullText || state.currentStreamText,
          });
          state.currentStreamId = null;
          state.currentStreamText = '';
          state.tokenEstimate = msg.tokenEstimate || state.tokenEstimate;
          if (msg.sessionName) state.sessionName = msg.sessionName;
          buildUI();
          break;

        case 'chat:error':
          state.streaming = false;
          state.messages.push({
            id: msg.messageId,
            role: 'error',
            content: msg.error || 'Unknown error',
          });
          state.currentStreamId = null;
          state.currentStreamText = '';
          buildUI();
          break;

        case 'chat:reset-ack':
          // Pick up new session name after reset
          if (msg.sessionName) state.sessionName = msg.sessionName;
          break;

        case 'chat:compact-ack':
          state.messages.push({
            id: genId(),
            role: 'system',
            content: 'Conversation compacted. ' + (msg.summary || ''),
          });
          buildUI();
          break;
      }
    };
  }

  function sendWS(data) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(data));
    }
  }

  function updateStreamMessage() {
    const el = document.getElementById('chat-stream-msg');
    if (el) {
      el.innerHTML = renderMarkdown(state.currentStreamText);
      const messagesDiv = document.getElementById('chat-messages');
      if (messagesDiv) messagesDiv.scrollTop = messagesDiv.scrollHeight;
    } else {
      // Need to add the stream element
      const messagesDiv = document.getElementById('chat-messages');
      if (!messagesDiv) return;
      const typing = document.getElementById('chat-typing');

      const streamEl = document.createElement('div');
      streamEl.className = 'chat-msg assistant';
      streamEl.id = 'chat-stream-msg';
      streamEl.innerHTML = renderMarkdown(state.currentStreamText);

      if (typing) {
        messagesDiv.insertBefore(streamEl, typing);
      } else {
        messagesDiv.appendChild(streamEl);
      }
      messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }
  }

  function updateActionIndicator(detail) {
    const messagesDiv = document.getElementById('chat-messages');
    if (!messagesDiv) return;

    let actionEl = document.getElementById('chat-action-indicator');
    if (!actionEl) {
      actionEl = document.createElement('div');
      actionEl.className = 'chat-action';
      actionEl.id = 'chat-action-indicator';
      const typing = document.getElementById('chat-typing');
      if (typing) {
        messagesDiv.insertBefore(actionEl, typing);
      } else {
        messagesDiv.appendChild(actionEl);
      }
    }
    actionEl.textContent = `Using ${detail}...`;
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
  }

  // ===== Helpers =====
  function genId() {
    return 'msg-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  }

  // ===== Integration with app.js =====
  // Expose a way for app.js to communicate the selected entry
  // We use MutationObserver on body dataset as a decoupled approach
  // But app.js doesn't set it — we'll hook into its click handler via a global
  window._chatSetSelectedEntry = function(id) {
    document.body.dataset.selectedEntryId = id || '';
  };

  // ===== Init =====
  connectWS();
  buildUI();
})();
