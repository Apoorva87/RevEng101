const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const RULES_FILE = path.join(__dirname, '..', '..', 'rules.json');

class RuleStore {
  constructor() {
    this.rules = [];
    this._load();
  }

  _load() {
    try {
      if (fs.existsSync(RULES_FILE)) {
        this.rules = JSON.parse(fs.readFileSync(RULES_FILE, 'utf8'));
      }
    } catch(e) {
      this.rules = [];
    }
  }

  _save() {
    try {
      fs.writeFileSync(RULES_FILE, JSON.stringify(this.rules, null, 2));
    } catch(e) {
      console.error('[RuleStore] Failed to save:', e.message);
    }
  }

  getAll() {
    return this.rules;
  }

  get(id) {
    return this.rules.find(r => r.id === id) || null;
  }

  add(rule) {
    const newRule = {
      id: crypto.randomUUID(),
      enabled: true,
      urlPattern: rule.urlPattern || '*',
      method: rule.method || '*',
      contentType: rule.contentType || '',
      headerKey: rule.headerKey || '',
      headerValue: rule.headerValue || '',
      direction: rule.direction || 'request', // request | response | both
      createdAt: Date.now(),
    };
    this.rules.push(newRule);
    this._save();
    return newRule;
  }

  update(id, updates) {
    const rule = this.rules.find(r => r.id === id);
    if (!rule) return null;
    Object.assign(rule, updates);
    this._save();
    return rule;
  }

  remove(id) {
    const idx = this.rules.findIndex(r => r.id === id);
    if (idx < 0) return false;
    this.rules.splice(idx, 1);
    this._save();
    return true;
  }
}

module.exports = RuleStore;
