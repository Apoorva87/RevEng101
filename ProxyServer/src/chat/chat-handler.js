const ClaudeSession = require('./claude-session');
const ContextBuilder = require('./context-builder');

class ChatHandler {
  constructor({ trafficStore, ruleStore, projectRoot }) {
    this.trafficStore = trafficStore;
    this.ruleStore = ruleStore;
    this.projectRoot = projectRoot;

    this.contextBuilder = new ContextBuilder({ trafficStore, ruleStore, projectRoot });
    this.session = null; // lazy-created
    this._clientToggles = new WeakMap(); // ws -> toggles
  }

  _getSession() {
    if (!this.session) {
      this.session = new ClaudeSession();
    }
    return this.session;
  }

  _getToggles(ws) {
    if (!this._clientToggles.has(ws)) {
      this._clientToggles.set(ws, {
        includeTraffic: true,
        includeSelected: false,
        includeRules: false,
        includeSource: false,
        includeBrowser: false,
      });
    }
    return this._clientToggles.get(ws);
  }

  handleMessage(ws, msg) {
    switch (msg.type) {
      case 'chat:send':
        return this._handleSend(ws, msg);
      case 'chat:reset':
        return this._handleReset(ws, msg);
      case 'chat:compact':
        return this._handleCompact(ws, msg);
      case 'chat:context-toggle':
        return this._handleContextToggle(ws, msg);
      case 'chat:browser-context':
        return this._handleBrowserContext(ws, msg);
      default:
        this._send(ws, { type: 'chat:error', messageId: msg.messageId, error: 'Unknown chat message type' });
    }
  }

  async _handleSend(ws, msg) {
    const session = this._getSession();
    const { messageId, text, selectedEntryId, contextToggles } = msg;

    if (!session.isAvailable()) {
      this._send(ws, {
        type: 'chat:error',
        messageId,
        error: 'Claude Code CLI not found. Install it and ensure "claude" is in your PATH.',
      });
      return;
    }

    // Update toggles if provided
    if (contextToggles) {
      this._clientToggles.set(ws, contextToggles);
    }

    const toggles = this._getToggles(ws);

    // Build dynamic context (traffic table, selected entry, rules, etc.)
    const { prompt: dynamicContext, tokenEstimate: dynamicTokens, breakdown } = this.contextBuilder.buildDynamicContext({
      ...toggles,
      selectedEntryId: selectedEntryId || null,
    });

    // On first message, also build the static preamble for --system-prompt
    let systemPrompt = null;
    if (session.isFirstMessage) {
      const { prompt: preamble } = this.contextBuilder.buildStaticPreamble();
      systemPrompt = preamble;
      breakdown.preamble = this.contextBuilder._estimateTokens(preamble);
    }

    // Wire up streaming events for this message
    const onChunk = (text) => {
      this._send(ws, { type: 'chat:chunk', messageId, text, done: false });
    };
    const onAction = (data) => {
      this._send(ws, { type: 'chat:action', messageId, action: data.action, detail: data.detail });
    };

    session.on('chunk', onChunk);
    session.on('action', onAction);

    try {
      const fullText = await session.send(text, {
        systemPrompt,
        dynamicContext: dynamicContext || null,
      });

      const tokenEstimate = Object.values(breakdown).reduce((a, b) => a + b, 0);
      this._send(ws, {
        type: 'chat:done',
        messageId,
        fullText,
        tokenEstimate,
        sessionName: session.sessionName,
      });
    } catch (err) {
      this._send(ws, {
        type: 'chat:error',
        messageId,
        error: err.message,
      });
    } finally {
      session.removeListener('chunk', onChunk);
      session.removeListener('action', onAction);
    }
  }

  _handleReset(ws, msg) {
    const session = this._getSession();
    session.reset();
    this._send(ws, {
      type: 'chat:reset-ack',
      messageId: msg.messageId,
      sessionName: session.sessionName,
    });
  }

  async _handleCompact(ws, msg) {
    const session = this._getSession();
    const summary = await session.compact();
    this._send(ws, { type: 'chat:compact-ack', messageId: msg.messageId, summary });
  }

  _handleContextToggle(ws, msg) {
    if (msg.toggles) {
      this._clientToggles.set(ws, msg.toggles);
    }
    this._sendStatus(ws);
  }

  _handleBrowserContext(ws, msg) {
    this.contextBuilder.setBrowserContext({
      url: msg.url,
      cookies: msg.cookies,
      localStorage: msg.localStorage,
    });
    this._sendStatus(ws);
  }

  sendStatus(ws) {
    this._sendStatus(ws);
  }

  _sendStatus(ws) {
    const session = this._getSession();
    const toggles = this._getToggles(ws);
    const { tokenEstimate, breakdown } = this.contextBuilder.buildSystemPrompt(toggles);

    this._send(ws, {
      type: 'chat:status',
      available: session.isAvailable(),
      sessionActive: session.sessionActive,
      sessionName: session.sessionName,
      contextToggles: toggles,
      tokenEstimate,
      breakdown,
    });
  }

  _send(ws, data) {
    try {
      if (ws.readyState === 1) { // WebSocket.OPEN
        ws.send(JSON.stringify(data));
      }
    } catch (e) {
      // Client disconnected
    }
  }

  kill() {
    if (this.session) {
      this.session.kill();
    }
    this.contextBuilder.destroy();
  }
}

module.exports = ChatHandler;
