class RuleEngine {
  constructor(ruleStore) {
    this.ruleStore = ruleStore;
  }

  matchRequest(entry) {
    const rules = this.ruleStore.getAll().filter(r =>
      r.enabled && (r.direction === 'request' || r.direction === 'both')
    );
    for (const rule of rules) {
      if (this._matches(rule, entry)) return rule;
    }
    return null;
  }

  matchResponse(entry) {
    const rules = this.ruleStore.getAll().filter(r =>
      r.enabled && (r.direction === 'response' || r.direction === 'both')
    );
    for (const rule of rules) {
      if (this._matches(rule, entry)) return rule;
    }
    return null;
  }

  _matches(rule, entry) {
    // URL pattern (glob-style)
    if (rule.urlPattern) {
      const regex = globToRegex(rule.urlPattern);
      if (!regex.test(entry.request.url)) return false;
    }

    // Method filter
    if (rule.method && rule.method !== '*') {
      if (entry.request.method.toUpperCase() !== rule.method.toUpperCase()) return false;
    }

    // Content-type filter
    if (rule.contentType) {
      const ct = (entry.request.contentType || entry.response.contentType || '').toLowerCase();
      if (!ct.includes(rule.contentType.toLowerCase())) return false;
    }

    // Header match
    if (rule.headerKey) {
      const headers = entry.request.headers;
      const val = headers[rule.headerKey.toLowerCase()];
      if (!val) return false;
      if (rule.headerValue && !val.includes(rule.headerValue)) return false;
    }

    return true;
  }
}

function globToRegex(pattern) {
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*')
    .replace(/\?/g, '.');
  return new RegExp('^' + escaped + '$', 'i');
}

module.exports = RuleEngine;
