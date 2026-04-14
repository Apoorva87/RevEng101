# Simplify Account Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the dashboard's account management by collapsing 5 modal tabs to 2, merging "Discover OAuth" into the scanner, persisting email in keychain payloads, and using email as the keychain account name for new entries.

**Architecture:** Two-tab modal (API Key with provider dropdown, Local OAuth with unified scanner). Email written into keychain JSON payloads on save/update, read back during discovery. Scanner (`scan_keychain_entries`) becomes the single discovery mechanism, replacing the separate `discover_claude_oauth` / `discover_codex_oauth` path on the web dashboard.

**Tech Stack:** Python 3.9+, vanilla JS (inline in HTML string), macOS `security` CLI

---

### Task 1: Persist email in keychain payloads (backend)

**Files:**
- Modify: `usage_hub.py:670-702` (refresh_claude_oauth)
- Modify: `usage_hub.py:658-667` (claude_keychain_payload)
- Modify: `usage_hub.py:481-507` (discover_claude_oauth)
- Modify: `usage_hub_web.py:302-356` (add_account)
- Modify: `usage_hub_web.py:358-401` (update_account)

**What:** When email is set on an AccountRecord backed by a keychain entry, write it into the keychain JSON payload as a top-level `"email"` key. When reading keychain entries (discovery, scan), read the email back out.

- [ ] **Step 1: Update `refresh_claude_oauth` to preserve email in payload**

In `usage_hub.py`, before the `security_store_password` call at line 697, inject the email from the record into the payload:

```python
def refresh_claude_oauth(session: requests.Session, record: AccountRecord, payload: dict[str, Any]) -> dict[str, Any]:
    # ... existing token refresh logic unchanged ...
    
    # Persist email in keychain payload
    if record.email:
        payload["email"] = record.email
    
    payload["claudeAiOauth"] = oauth
    security_store_password(
        record.keychain_service or CLAUDE_KEYCHAIN_SERVICE,
        record.keychain_account or getpass.getuser(),
        json.dumps(payload),
    )
    return payload
```

- [ ] **Step 2: Update `discover_claude_oauth` to read email from keychain**

In `usage_hub.py`, after parsing the payload, extract the email:

```python
def discover_claude_oauth() -> Optional[AccountRecord]:
    account = getpass.getuser()
    raw = security_find_password(CLAUDE_KEYCHAIN_SERVICE, account)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    oauth = payload.get("claudeAiOauth") or {}
    if not str(oauth.get("accessToken") or "").strip():
        return None
    return AccountRecord(
        id="claude-oauth-keychain",
        name="Claude Local OAuth",
        provider="claude",
        auth_kind="oauth",
        visible=True,
        default_model=DEFAULT_CLAUDE_MODELS[0],
        models=list(DEFAULT_CLAUDE_MODELS),
        keychain_service=CLAUDE_KEYCHAIN_SERVICE,
        keychain_account=account,
        email=str(payload.get("email") or "").strip() or None,
        token_url=CLAUDE_TOKEN_URL,
        client_id=discover_claude_client_id() or "",
        source="discovered",
        user_added=False,
    )
```

- [ ] **Step 3: Update `scan_keychain_entries` to extract email from payloads**

In `usage_hub_web.py`, add `"email"` to the entry dict inside `scan_keychain_entries`:

```python
entry: dict[str, Any] = {
    "service": current_svce,
    "account": current_acct,
    "has_oauth": False,
    "subscription_type": None,
    "token_preview": None,
    "provider": None,
    "email": None,
    "command": f"security find-generic-password -s {current_svce!r} -a {current_acct!r} -w",
}
raw = security_find_password(current_svce, current_acct)
if raw:
    try:
        payload = json.loads(raw)
        # Read email from top-level payload
        entry["email"] = str(payload.get("email") or "").strip() or None
        oauth = _extract_oauth_from_payload(payload)
        if oauth:
            # ... existing oauth extraction ...
```

- [ ] **Step 4: Update `add_account` to write email into keychain on creation**

In `usage_hub_web.py`, after the Claude OAuth record is built (around line 335-342), if email is set, write it into the keychain payload:

```python
        elif provider == "claude":
            record.keychain_service = str(payload.get("keychain_service") or CLAUDE_KEYCHAIN_SERVICE).strip()
            record.keychain_account = str(payload.get("keychain_account") or "").strip()
            if not record.keychain_account:
                raise ValueError("keychain_account is required for Claude OAuth")
            record.token_url = str(payload.get("token_url") or CLAUDE_TOKEN_URL).strip()
            record.client_id = str(payload.get("client_id") or discover_claude_client_id() or "").strip()
            if not record.client_id:
                raise ValueError("client_id is required for Claude OAuth")
            # Persist email into keychain payload
            if record.email and record.keychain_service and record.keychain_account:
                kc_raw = security_find_password(record.keychain_service, record.keychain_account)
                if kc_raw:
                    try:
                        kc_payload = json.loads(kc_raw)
                        kc_payload["email"] = record.email
                        security_store_password(record.keychain_service, record.keychain_account, json.dumps(kc_payload))
                    except (json.JSONDecodeError, RuntimeError):
                        pass
```

Add import of `security_store_password` from `usage_hub` at the top of `usage_hub_web.py`.

- [ ] **Step 5: Update `update_account` to sync email to keychain on PATCH**

In `usage_hub_web.py`, after the simple_fields loop in `update_account`, add keychain sync:

```python
            # Sync email to keychain if this is a keychain-backed account
            if "email" in payload and record.keychain_service and record.keychain_account:
                kc_raw = security_find_password(record.keychain_service, record.keychain_account)
                if kc_raw:
                    try:
                        kc_payload = json.loads(kc_raw)
                        email_val = str(payload.get("email") or "").strip()
                        if email_val:
                            kc_payload["email"] = email_val
                        else:
                            kc_payload.pop("email", None)
                        security_store_password(record.keychain_service, record.keychain_account, json.dumps(kc_payload))
                    except (json.JSONDecodeError, RuntimeError):
                        pass
```

- [ ] **Step 6: Verify**

Run: `python3 -c "from usage_hub_web import scan_keychain_entries; from usage_hub import discover_claude_oauth; print('imports OK')"`

---

### Task 2: Remove "Discover OAuth" button, merge into scanner

**Files:**
- Modify: `usage_hub_web.py:735-749` (header HTML)
- Modify: `usage_hub_web.py:1464-1467` (discover click handler JS)
- Modify: `usage_hub_web.py:804` (empty state text)

Note: Keep the `/api/discover` endpoint and `discover_now()` method — they're still used by `reload_accounts` on startup. Just remove the UI button.

- [ ] **Step 1: Remove "Discover OAuth" button from header HTML**

Replace the header-actions div (lines ~738-749):

```html
      <div class="header-actions">
        <button class="btn btn-primary" id="refresh-all">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Refresh All
        </button>
        <button class="btn btn-ghost" id="add-account">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Add Account
        </button>
      </div>
```

- [ ] **Step 2: Remove the discover click handler JS**

Delete these lines from the `init()` function:

```javascript
      el('discover').addEventListener('click', async () => {
        const data = await api('/api/discover', { method: 'POST' });
        await loadSnapshot(true);
        flash(data.added?.length ? `Discovered ${data.added.length} new account(s)` : 'No new local OAuth accounts found', 3000, data.added?.length ? 'success' : 'info');
      });
```

- [ ] **Step 3: Update empty state text**

Change the empty state message in cards div and renderCards:

```
No accounts yet. Click "Add Account" to get started.
```

(Remove references to "Discover OAuth")

- [ ] **Step 4: Verify HTML renders**

Run: `python3 -c "from usage_hub_web import html_page; print(f'OK: {len(html_page())} chars')"`

---

### Task 3: Collapse 5 modal tabs into 2 (API Key + Local OAuth)

**Files:**
- Modify: `usage_hub_web.py:831-837` (modal switcher HTML)
- Modify: `usage_hub_web.py:860-923` (formModes JS object)
- Modify: `usage_hub_web.py:1263-1295` (renderForm function)
- Modify: `usage_hub_web.py:1394-1403` (collectFormPayload function)
- Modify: `usage_hub_web.py:1442` (add-account click opens default mode)

- [ ] **Step 1: Replace the modal switcher HTML**

Replace the 5-button switcher (lines ~831-837) with 2 tabs:

```html
      <div class="switcher">
        <button class="btn btn-primary btn-sm" data-mode="api-key">API Key</button>
        <button class="btn btn-ghost btn-sm" data-mode="local-oauth">Local OAuth</button>
      </div>
```

- [ ] **Step 2: Replace formModes with unified API Key mode**

Replace the entire `formModes` object with a single `api-key` mode that has a provider dropdown:

```javascript
    const formModes = {
      'api-key': {
        title: 'Add API Key Account',
        subtitle: 'Monitor usage via API key (Claude or OpenAI).',
        fields: [
          ['provider', 'Provider', 'select', 'claude', [['claude', 'Claude (Anthropic)'], ['codex', 'OpenAI']]],
          ['name', 'Account label', 'text', ''],
          ['email', 'Email / Gmail', 'email', ''],
          ['api_key', 'API key', 'password', ''],
          ['models', 'Models (comma separated)', 'textarea', ''],
          ['api_base', 'API base', 'text', ''],
          ['refresh_interval', 'Refresh seconds', 'number', ''],
        ],
        payload: { auth_kind: 'api' },
      },
    };

    const providerDefaults = {
      claude: { name: 'Claude API', models: 'claude-opus-4-6, claude-haiku-4-5-20251001', api_base: 'https://api.anthropic.com' },
      codex:  { name: 'OpenAI API', models: 'gpt-4.1-mini, gpt-4.1', api_base: 'https://api.openai.com' },
    };
```

- [ ] **Step 3: Update renderForm to handle select fields + provider switching + local-oauth mode**

Replace the `renderForm` function:

```javascript
    function renderForm() {
      document.querySelectorAll('[data-mode]').forEach((btn) => {
        btn.className = btn.dataset.mode === state.mode ? 'btn btn-primary btn-sm' : 'btn btn-ghost btn-sm';
      });
      const grid = el('form-grid');
      const footer = document.querySelector('.modal-footer');

      if (state.mode === 'local-oauth') {
        el('modal-title').textContent = 'Scan Local Credentials';
        el('modal-subtitle').textContent = 'Scanning keychain and local files for OAuth entries...';
        grid.innerHTML = '<div class="muted" style="padding:1rem;text-align:center;">Scanning...</div>';
        if (footer) footer.style.display = 'none';
        scanKeychain();
        return;
      }

      if (footer) footer.style.display = '';
      const cfg = formModes[state.mode];
      if (!cfg) return;
      el('modal-title').textContent = cfg.title;
      el('modal-subtitle').textContent = cfg.subtitle;
      grid.innerHTML = '';
      cfg.fields.forEach(([name, label, type, defaultVal, options]) => {
        const field = document.createElement('div');
        field.className = 'field' + (type === 'textarea' ? ' full' : '');
        let input;
        if (type === 'select') {
          const opts = (options || []).map(([v, l]) => `<option value="${v}"${v === defaultVal ? ' selected' : ''}>${l}</option>`).join('');
          input = `<select id="field-${name}">${opts}</select>`;
        } else if (type === 'textarea') {
          input = `<textarea id="field-${name}" placeholder="${defaultVal}"></textarea>`;
        } else {
          input = `<input id="field-${name}" type="${type}" placeholder="${defaultVal}" value="${type !== 'password' ? defaultVal : ''}">`;
        }
        field.innerHTML = `<label for="field-${name}">${label}</label>${input}`;
        grid.appendChild(field);
      });

      // Apply provider defaults and wire up provider change
      applyProviderDefaults();
      const providerField = document.getElementById('field-provider');
      if (providerField) {
        providerField.addEventListener('change', applyProviderDefaults);
      }
    }

    function applyProviderDefaults() {
      const providerField = document.getElementById('field-provider');
      if (!providerField) return;
      const provider = providerField.value;
      const defaults = providerDefaults[provider] || {};
      ['name', 'models', 'api_base'].forEach((key) => {
        const node = document.getElementById(`field-${key}`);
        if (node && !node.dataset.userEdited) {
          if (node.tagName === 'TEXTAREA') {
            node.placeholder = defaults[key] || '';
            if (!node.value) node.value = '';
          } else {
            node.value = defaults[key] || '';
          }
        }
      });
    }
```

- [ ] **Step 4: Update collectFormPayload to include provider from dropdown**

```javascript
    function collectFormPayload() {
      const cfg = formModes[state.mode];
      if (!cfg) return {};
      const payload = { ...cfg.payload };
      cfg.fields.forEach(([name]) => {
        const node = el(`field-${name}`);
        if (!node) return;
        payload[name] = node.value;
      });
      // Use provider defaults for empty optional fields
      const defaults = providerDefaults[payload.provider] || {};
      if (!payload.models) payload.models = defaults.models || '';
      if (!payload.api_base) payload.api_base = defaults.api_base || '';
      if (!payload.name) payload.name = defaults.name || '';
      return payload;
    }
```

- [ ] **Step 5: Update default mode**

Change the default mode in `state` and the `add-account` click handler:

```javascript
    const state = {
      snapshot: null,
      selectedId: null,
      mode: 'api-key',
      // ...
    };

    // In init():
    el('add-account').addEventListener('click', () => openModal('api-key'));
```

- [ ] **Step 6: Verify HTML renders**

Run: `python3 -c "from usage_hub_web import html_page; print(f'OK: {len(html_page())} chars')"`

---

### Task 4: Update scanKeychain to pre-fill email + show "Add All" button

**Files:**
- Modify: `usage_hub_web.py` (scanKeychain function)

- [ ] **Step 1: Update scanKeychain to pre-fill email from keychain payload**

In the entry rendering loop, pre-fill the email input from `entry.email`:

```javascript
          const inputRow = entry.has_oauth ? `
            <div style="display:flex;gap:0.5rem;margin-top:0.5rem;flex-wrap:wrap;">
              <input id="${nameId}" type="text" placeholder="Account label" value="${escapeHtml(defaultName)}"
                style="flex:1 1 140px;min-width:120px;font-size:0.75rem;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);">
              <input id="${emailId}" type="email" placeholder="Email / Gmail" value="${escapeHtml(entry.email || '')}"
                style="flex:1 1 160px;min-width:120px;font-size:0.75rem;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);">
              <button class="btn btn-primary btn-sm kc-add" data-idx="${idx}" style="font-size:0.7rem;padding:4px 12px;white-space:nowrap;">Add</button>
            </div>
          ` : '';
```

- [ ] **Step 2: Add "Add All" button at the top of scanner results**

After the command block and before the entries loop, add an "Add All" button when there are multiple valid entries:

```javascript
        const validEntries = entries.filter(e => e.has_oauth);
        if (validEntries.length > 1) {
          const addAllRow = document.createElement('div');
          addAllRow.style.cssText = 'display:flex;justify-content:flex-end;margin-bottom:0.5rem;';
          addAllRow.innerHTML = `<button class="btn btn-primary btn-sm" id="kc-add-all" style="font-size:0.7rem;">Add All (${validEntries.length})</button>`;
          grid.appendChild(addAllRow);
        }
```

Wire up the Add All button after the entries loop:

```javascript
        const addAllBtn = document.getElementById('kc-add-all');
        if (addAllBtn) {
          addAllBtn.addEventListener('click', async () => {
            let added = 0;
            for (let idx = 0; idx < entries.length; idx++) {
              const entry = entries[idx];
              if (!entry.has_oauth) continue;
              const name = document.getElementById(`kc-name-${idx}`)?.value?.trim() || entry.service;
              const email = document.getElementById(`kc-email-${idx}`)?.value?.trim() || '';
              try {
                await api('/api/accounts', { method: 'POST', body: {
                  provider: entry.provider || 'claude', auth_kind: 'oauth',
                  name, email: email || undefined,
                  keychain_service: entry.service,
                  keychain_account: entry.account,
                  client_id: clientId,
                }});
                added++;
              } catch (err) { /* skip duplicates */ }
            }
            closeModal();
            await loadSnapshot(true);
            flash(`Added ${added} account(s)`, 2500, 'success');
          });
        }
```

- [ ] **Step 3: Verify**

Run: `python3 -c "from usage_hub_web import html_page; print(f'OK: {len(html_page())} chars')"`

---

### Task 5: Use email as keychain account name for new entries

**Files:**
- Modify: `usage_hub_web.py:302-356` (add_account method)

- [ ] **Step 1: Update add_account to use email as keychain_account when provided**

In the Claude OAuth branch of `add_account`, if the user provides an email but no explicit keychain_account, use the email:

```python
        elif provider == "claude":
            record.keychain_service = str(payload.get("keychain_service") or CLAUDE_KEYCHAIN_SERVICE).strip()
            record.keychain_account = str(payload.get("keychain_account") or "").strip()
            # If keychain_account not explicitly set but email is, use email
            if not record.keychain_account and record.email:
                record.keychain_account = record.email
            if not record.keychain_account:
                raise ValueError("keychain_account or email is required for Claude OAuth")
            # ... rest unchanged ...
```

Note: This only applies to **new** accounts created through the dashboard. Existing accounts and the primary Claude Code entry retain their original keychain_account (OS username).

- [ ] **Step 2: Verify**

Run: `python3 -c "from usage_hub_web import DashboardApp; print('OK')"`

---

### Task 6: Integration test and cleanup

**Files:**
- Modify: `usage_hub_web.py` (minor cleanups)

- [ ] **Step 1: Remove stale references to "Discover OAuth" in empty state messages**

Search for remaining references to "Discover OAuth" in the HTML/JS and update them.

- [ ] **Step 2: Start the server and test the full flow**

```bash
python3 usage_hub_web.py --no-browser --no-initial-refresh --port 8799
```

Test:
1. `curl http://127.0.0.1:8799/` — verify HTML loads, 2 tabs visible
2. `curl http://127.0.0.1:8799/api/keychain/scan` — verify entries have email field
3. POST a new account with email via API, verify email persists in keychain
4. PATCH email on existing account, verify keychain updated
5. Verify existing accounts with emails still show correctly

- [ ] **Step 3: Commit**

```bash
git add usage_hub.py usage_hub_web.py
git commit -m "Simplify account management: 2-tab modal, email in keychain, unified scanner"
```
