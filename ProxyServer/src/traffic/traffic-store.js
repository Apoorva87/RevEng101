const EventEmitter = require('events');

const DEFAULT_MAX_SIZE = 5000;

class TrafficStore extends EventEmitter {
  constructor(maxSize = DEFAULT_MAX_SIZE) {
    super();
    this.maxSize = maxSize;
    this.entries = new Map(); // id -> TrafficEntry
    this.order = [];          // ordered list of ids
  }

  add(entry) {
    if (this.order.length >= this.maxSize) {
      const oldId = this.order.shift();
      this.entries.delete(oldId);
    }
    this.entries.set(entry.id, entry);
    this.order.push(entry.id);
    this.emit('add', entry);
    return entry;
  }

  get(id) {
    return this.entries.get(id) || null;
  }

  update(id) {
    const entry = this.entries.get(id);
    if (entry) {
      this.emit('update', entry);
    }
    return entry;
  }

  getAll() {
    return this.order.map(id => this.entries.get(id)).filter(Boolean);
  }

  getAllSummaries() {
    return this.order.map(id => {
      const e = this.entries.get(id);
      return e ? e.toSummary() : null;
    }).filter(Boolean);
  }

  clear() {
    this.entries.clear();
    this.order = [];
    this.emit('clear');
  }

  get size() {
    return this.entries.size;
  }
}

module.exports = TrafficStore;
