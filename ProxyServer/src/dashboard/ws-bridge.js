const WebSocket = require('ws');

class WSBridge {
  constructor({ server, trafficStore, chatHandler }) {
    this.trafficStore = trafficStore;
    this.chatHandler = chatHandler || null;
    this.wss = new WebSocket.Server({ server });
    this.clients = new Set();

    this.wss.on('connection', (ws) => {
      this.clients.add(ws);
      ws.on('close', () => this.clients.delete(ws));
      ws.on('error', () => this.clients.delete(ws));

      // Send current traffic count on connect
      ws.send(JSON.stringify({
        type: 'init',
        count: trafficStore.size,
      }));

      // Send chat status on connect
      if (this.chatHandler) {
        this.chatHandler.sendStatus(ws);
      }

      // Route incoming messages
      ws.on('message', (data) => {
        try {
          const msg = JSON.parse(data.toString());
          if (msg.type && msg.type.startsWith('chat:') && this.chatHandler) {
            this.chatHandler.handleMessage(ws, msg);
          }
        } catch (e) {
          // Ignore malformed messages
        }
      });
    });

    // Listen to traffic store events
    trafficStore.on('add', entry => {
      this._broadcast({ type: 'add', entry: entry.toSummary() });
    });

    trafficStore.on('update', entry => {
      this._broadcast({ type: 'update', entry: entry.toSummary() });
    });

    trafficStore.on('clear', () => {
      this._broadcast({ type: 'clear' });
    });
  }

  _broadcast(data) {
    const msg = JSON.stringify(data);
    for (const ws of this.clients) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(msg);
      }
    }
  }

  get connectionCount() {
    return this.clients.size;
  }
}

module.exports = WSBridge;
