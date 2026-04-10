const TrafficEntry = require('../traffic/traffic-entry');

const AUTO_FORWARD_TIMEOUT = 5 * 60 * 1000; // 5 minutes

class ResponseInterceptor {
  constructor({ ruleEngine, trafficStore }) {
    this.ruleEngine = ruleEngine;
    this.trafficStore = trafficStore;
    this.enabled = false;
    this.pending = new Map(); // entryId -> { resolve, reject, timer }

    // Listen for forward/drop events from dashboard (distinct from request events)
    trafficStore.on('forward-response', (entryId, modifications) => {
      this._resolve(entryId, modifications);
    });
    trafficStore.on('drop-response', (entryId) => {
      this._reject(entryId);
    });
  }

  async checkResponse(entry, { statusCode, headers, body }) {
    if (!this.enabled) return null;

    const matchedRule = this.ruleEngine.matchResponse(entry);
    if (!matchedRule) return null;

    // Hold the response
    entry.state = TrafficEntry.STATES.INTERCEPTED;
    entry.intercept.wasIntercepted = true;
    entry.intercept.matchedRuleId = matchedRule.id;
    entry.intercept.phase = 'response';
    this.trafficStore.update(entry.id);

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        // Auto-forward on timeout
        this.pending.delete(entry.id);
        resolve(null); // Forward unmodified
      }, AUTO_FORWARD_TIMEOUT);

      this.pending.set(entry.id, { resolve, reject, timer });
    });
  }

  _resolve(entryId, modifications) {
    const p = this.pending.get(entryId);
    if (!p) return;
    clearTimeout(p.timer);
    this.pending.delete(entryId);

    const entry = this.trafficStore.get(entryId);
    if (entry && modifications && Object.keys(modifications).length > 0) {
      entry.intercept.wasModified = true;
      this.trafficStore.update(entryId);
    }

    p.resolve(modifications && Object.keys(modifications).length > 0 ? modifications : null);
  }

  _reject(entryId) {
    const p = this.pending.get(entryId);
    if (!p) return;
    clearTimeout(p.timer);
    this.pending.delete(entryId);

    const entry = this.trafficStore.get(entryId);
    if (entry) {
      entry.abort();
      this.trafficStore.update(entryId);
    }

    p.reject(new Error('Response dropped by user'));
  }

  get pendingCount() {
    return this.pending.size;
  }
}

module.exports = ResponseInterceptor;
