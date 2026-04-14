#!/usr/bin/env python3
"""Local web dashboard for Claude and Codex/OpenAI usage accounts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import uuid
import webbrowser
import warnings
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+",
)

import requests

from usage_hub import (
    CLAUDE_API_BASE,
    CLAUDE_KEYCHAIN_SERVICE,
    CLAUDE_TOKEN_URL,
    CODEX_AUTH_FILE,
    CODEX_CLIENT_ID,
    CODEX_SESSIONS_DIR,
    CODEX_TOKEN_URL,
    CONFIG_PATH,
    DEFAULT_CLAUDE_MODELS,
    DEFAULT_GLOBAL_REFRESH,
    DEFAULT_OPENAI_MODELS,
    OPENAI_API_BASE,
    AccountRecord,
    AccountState,
    ConfigStore,
    account_snapshot,
    account_status_text,
    account_window_summary,
    build_states,
    compact_error,
    discover_claude_client_id,
    discover_claude_oauth,
    discover_codex_oauth,
    security_find_password,
    format_countdown,
    format_relative,
    merge_accounts,
    normalize_builtin_accounts,
    now_ts,
    refresh_state,
    slugify,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive local web dashboard for Claude and Codex/OpenAI usage.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"Config file path. Default: {CONFIG_PATH}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Default: 8765")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the dashboard in a browser.")
    parser.add_argument("--no-initial-refresh", action="store_true", help="Skip refreshing all accounts on startup.")
    return parser.parse_args()


def mask_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def normalize_models(raw: Any, provider: str) -> list[str]:
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(item).strip() for item in raw]
    else:
        items = []
    models = [item for item in items if item]
    if models:
        return models
    return list(DEFAULT_CLAUDE_MODELS[:2] if provider == "claude" else DEFAULT_OPENAI_MODELS[:2])


MAX_EVENT_LOG_ENTRIES = 200


_KEYCHAIN_KEYWORDS = ("claude", "codex", "anthropic", "openai")


def _extract_oauth_from_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Try to extract claudeAiOauth from a keychain payload, handling base64 wrappers."""
    oauth = (payload.get("claudeAiOauth") or {})
    if oauth and str(oauth.get("accessToken") or "").strip():
        return oauth
    # CodexBar-style wrapper: {"storedAt":..., "owner":..., "data": "<base64 json>"}
    b64data = str(payload.get("data") or "").strip()
    if b64data:
        try:
            import base64
            decoded = json.loads(base64.b64decode(b64data))
            if isinstance(decoded, dict):
                oauth = decoded.get("claudeAiOauth") or {}
                if oauth and str(oauth.get("accessToken") or "").strip():
                    return oauth
        except Exception:
            pass
    # Codex CLI tokens structure: {"tokens": {"access_token":..., "account_id":...}}
    tokens = payload.get("tokens") or {}
    if str(tokens.get("access_token") or "").strip():
        token = str(tokens["access_token"]).strip()
        return {
            "accessToken": token,
            "subscriptionType": None,
            "_provider": "codex",
        }
    return None


def scan_keychain_entries() -> list[dict[str, Any]]:
    """Scan the macOS keychain for entries that look like Claude or Codex OAuth credentials."""
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    current_svce: Optional[str] = None
    current_acct: Optional[str] = None

    for line in result.stdout.splitlines():
        line = line.strip()
        match_svce = re.match(r'"svce"<blob>="(.+)"', line)
        match_acct = re.match(r'"acct"<blob>="(.+)"', line)
        if match_svce:
            current_svce = match_svce.group(1)
        elif match_acct:
            current_acct = match_acct.group(1)
        elif line.startswith("class:") or line.startswith("keychain:"):
            if current_svce and current_acct:
                key = (current_svce, current_acct)
                combined = (current_svce + current_acct).lower()
                is_relevant = any(kw in combined for kw in _KEYCHAIN_KEYWORDS)
                if is_relevant and key not in seen:
                    seen.add(key)
                    entry: dict[str, Any] = {
                        "service": current_svce,
                        "account": current_acct,
                        "has_oauth": False,
                        "subscription_type": None,
                        "token_preview": None,
                        "provider": None,
                        "command": f"security find-generic-password -s {current_svce!r} -a {current_acct!r} -w",
                    }
                    raw = security_find_password(current_svce, current_acct)
                    if raw:
                        try:
                            payload = json.loads(raw)
                            oauth = _extract_oauth_from_payload(payload)
                            if oauth:
                                token = str(oauth.get("accessToken") or "").strip()
                                entry["has_oauth"] = True
                                entry["subscription_type"] = oauth.get("subscriptionType")
                                entry["provider"] = oauth.get("_provider", "claude")
                                entry["token_preview"] = f"{token[:12]}...{token[-6:]}" if len(token) > 18 else token[:8] + "..."
                        except (json.JSONDecodeError, TypeError):
                            pass
                    entries.append(entry)
            current_svce = None
            current_acct = None

    return entries


class DashboardApp:
    def __init__(self, config_path: Path):
        self.config_path = config_path.expanduser()
        self.lock = threading.RLock()
        self.session = requests.Session()
        self.store = ConfigStore(self.config_path)
        self.states: list[AccountState] = []
        self.event_log: list[dict[str, Any]] = []
        self.reload_accounts()

    def reload_accounts(self) -> None:
        with self.lock:
            self.store.load()
            discovered = [item for item in [discover_codex_oauth(), discover_claude_oauth()] if item]
            accounts = merge_accounts(self.store.accounts, discovered)
            normalize_builtin_accounts(accounts)
            if accounts and not any(account.visible for account in accounts):
                for account in accounts:
                    account.visible = True
            self.store.accounts = accounts
            self.states = build_states(accounts)
            self.store.save()

    def _state_map(self) -> dict[str, AccountState]:
        return {state.record.id: state for state in self.states}

    def _append_event(
        self,
        message: str,
        event_type: str = "info",
        *,
        account_id: Optional[str] = None,
        account_name: Optional[str] = None,
        source: str = "server",
    ) -> None:
        self.event_log.append(
            {
                "at": now_ts(),
                "type": event_type,
                "message": message,
                "account_id": account_id,
                "account_name": account_name,
                "source": source,
            }
        )
        if len(self.event_log) > MAX_EVENT_LOG_ENTRIES:
            self.event_log = self.event_log[-MAX_EVENT_LOG_ENTRIES:]

    def _log_refresh_event(self, state: AccountState, action: str) -> None:
        detail = state.error or account_snapshot(state) or account_status_text(state)
        outcome = "failed" if state.error else "passed"
        event_type = "error" if state.error else "success"
        self._append_event(
            f"{action} {outcome}: {state.record.name} | {detail}",
            event_type,
            account_id=state.record.id,
            account_name=state.record.name,
        )

    def refresh_due(self) -> None:
        with self.lock:
            for state in self.states:
                if state.last_refresh_finished_at is None or state.next_refresh_at <= now_ts():
                    refresh_state(self.session, state, self.store.global_refresh_interval)
                    self._log_refresh_event(state, "auto refresh")

    def refresh_all(self) -> None:
        with self.lock:
            for state in self.states:
                refresh_state(self.session, state, self.store.global_refresh_interval)
                self._log_refresh_event(state, "manual refresh")

    def refresh_account(self, account_id: str) -> None:
        with self.lock:
            state = self._state_map().get(account_id)
            if not state:
                raise KeyError(f"Unknown account: {account_id}")
            refresh_state(self.session, state, self.store.global_refresh_interval)
            self._log_refresh_event(state, "manual refresh")

    def discover_now(self) -> list[str]:
        with self.lock:
            before = {account.id for account in self.store.accounts}
            discovered = [item for item in [discover_codex_oauth(), discover_claude_oauth()] if item]
            accounts = merge_accounts(self.store.accounts, discovered)
            normalize_builtin_accounts(accounts)
            self.store.accounts = accounts
            self.store.save()
            by_id = self._state_map()
            refreshed: list[AccountState] = []
            for account in accounts:
                state = by_id.get(account.id) or AccountState(record=account)
                state.record = account
                refreshed.append(state)
            self.states = refreshed
            added = [account.id for account in accounts if account.id not in before]
            if added:
                self._append_event(f"Discovery added {len(added)} account(s).", "success")
            else:
                self._append_event("Discovery completed with no new accounts.", "info")
            return added

    def _save_store(self) -> None:
        self.store.save()

    def add_account(self, payload: dict[str, Any]) -> AccountRecord:
        provider = str(payload.get("provider") or "").strip().lower()
        auth_kind = str(payload.get("auth_kind") or "").strip().lower()
        if provider not in {"claude", "codex"}:
            raise ValueError("provider must be 'claude' or 'codex'")
        if auth_kind not in {"api", "oauth"}:
            raise ValueError("auth_kind must be 'api' or 'oauth'")

        name = str(payload.get("name") or "").strip() or f"{provider.title()} {auth_kind.upper()}"
        models = ["account-usage"] if (provider == "codex" and auth_kind == "oauth") else normalize_models(payload.get("models"), provider)
        default_model = models[0] if models else None
        record = AccountRecord(
            id=f"{provider}-{auth_kind}-{slugify(name)}-{uuid.uuid4().hex[:6]}",
            name=name,
            provider=provider,
            auth_kind=auth_kind,
            enabled=bool(payload.get("enabled", True)),
            visible=bool(payload.get("visible", True)),
            refresh_interval=float(payload["refresh_interval"]) if payload.get("refresh_interval") not in {None, ""} else None,
            default_model=default_model,
            models=models,
            email=str(payload.get("email") or "").strip() or None,
            source="saved",
            user_added=True,
        )

        if auth_kind == "api":
            api_key = str(payload.get("api_key") or "").strip()
            if not api_key:
                raise ValueError("api_key is required for API accounts")
            record.api_key = api_key
            record.api_base = str(payload.get("api_base") or (CLAUDE_API_BASE if provider == "claude" else OPENAI_API_BASE)).strip()
        elif provider == "claude":
            record.keychain_service = str(payload.get("keychain_service") or CLAUDE_KEYCHAIN_SERVICE).strip()
            record.keychain_account = str(payload.get("keychain_account") or "").strip()
            if not record.keychain_account:
                raise ValueError("keychain_account is required for Claude OAuth")
            record.token_url = str(payload.get("token_url") or CLAUDE_TOKEN_URL).strip()
            record.client_id = str(payload.get("client_id") or discover_claude_client_id() or "").strip()
            if not record.client_id:
                raise ValueError("client_id is required for Claude OAuth")
        else:
            record.auth_file = str(payload.get("auth_file") or CODEX_AUTH_FILE).strip()
            record.sessions_dir = str(payload.get("sessions_dir") or CODEX_SESSIONS_DIR).strip()
            record.token_url = str(payload.get("token_url") or CODEX_TOKEN_URL).strip()
            record.client_id = str(payload.get("client_id") or CODEX_CLIENT_ID).strip()
            if not record.auth_file:
                raise ValueError("auth_file is required for Codex OAuth")

        with self.lock:
            self.store.accounts.append(record)
            self.states.append(AccountState(record=record))
            self._save_store()
            self._append_event(f"Added account: {record.name}", "success", account_id=record.id, account_name=record.name)
        return record

    def update_account(self, account_id: str, payload: dict[str, Any]) -> AccountRecord:
        with self.lock:
            state = self._state_map().get(account_id)
            if not state:
                raise KeyError(f"Unknown account: {account_id}")
            record = state.record

            simple_fields = {
                "name": str,
                "email": str,
                "enabled": bool,
                "visible": bool,
                "api_base": str,
                "auth_file": str,
                "sessions_dir": str,
                "keychain_service": str,
                "keychain_account": str,
                "token_url": str,
                "client_id": str,
            }
            for field_name, cast in simple_fields.items():
                if field_name in payload:
                    value = payload[field_name]
                    setattr(record, field_name, cast(value) if value not in {None, ""} else None)

            if "refresh_interval" in payload:
                value = payload.get("refresh_interval")
                record.refresh_interval = float(value) if value not in {None, ""} else None
            if "api_key" in payload:
                api_key = str(payload.get("api_key") or "").strip()
                if api_key:
                    record.api_key = api_key
            if "models" in payload:
                models = ["account-usage"] if (record.provider == "codex" and record.auth_kind == "oauth") else normalize_models(payload.get("models"), record.provider)
                record.models = models
                record.default_model = models[0] if models else None

            for index, existing in enumerate(self.store.accounts):
                if existing.id == account_id:
                    self.store.accounts[index] = record
                    break
            self._save_store()
            self._append_event(f"Updated account: {record.name}", "info", account_id=record.id, account_name=record.name)
            return record

    def delete_account(self, account_id: str) -> None:
        with self.lock:
            state = self._state_map().get(account_id)
            if not state:
                raise KeyError(f"Unknown account: {account_id}")
            deleted_name = state.record.name
            self.store.accounts = [account for account in self.store.accounts if account.id != account_id]
            self.states = [item for item in self.states if item.record.id != account_id]
            self._save_store()
            self._append_event(f"Deleted account: {deleted_name}", "info", account_id=account_id, account_name=deleted_name)

    def set_global_refresh(self, seconds: float) -> float:
        with self.lock:
            self.store.global_refresh_interval = max(5.0, min(600.0, seconds))
            self._save_store()
            self._append_event(
                f"Global refresh interval set to {self.store.global_refresh_interval:.0f}s.",
                "info",
            )
            return self.store.global_refresh_interval

    def _serialize_probe(self, probe: Any) -> dict[str, Any]:
        if probe is None:
            return {}
        return {
            "model": probe.model,
            "ok": probe.ok,
            "status": probe.status,
            "checked_at": probe.checked_at,
            "response_usage": probe.response_usage,
            "rate_limits": probe.rate_limits,
            "extra": probe.extra,
            "error": probe.error,
        }

    def _serialize_account(self, state: AccountState) -> dict[str, Any]:
        p5, p5_text, r5 = account_window_summary(state, "5h")
        p7, p7_text, r7 = account_window_summary(state, "7d")
        record = asdict(state.record)
        record["api_key"] = None
        record["api_key_masked"] = mask_secret(state.record.api_key)
        return {
            "id": state.record.id,
            "record": record,
            "summary": state.summary,
            "models": {name: self._serialize_probe(probe) for name, probe in state.models.items()},
            "active_model": state.active_model_name(),
            "snapshot": account_snapshot(state),
            "status_text": account_status_text(state),
            "error": state.error,
            "last_refresh_started_at": state.last_refresh_started_at,
            "last_refresh_finished_at": state.last_refresh_finished_at,
            "last_success_at": state.last_success_at,
            "last_success_relative": format_relative(state.last_success_at),
            "next_refresh_at": state.next_refresh_at,
            "next_refresh_in": format_countdown(max(0, (state.next_refresh_at or 0) - now_ts())),
            "windows": {
                "5h": {"percent": p5, "text": p5_text, "reset": r5},
                "7d": {"percent": p7, "text": p7_text, "reset": r7},
            },
        }

    def snapshot(self) -> dict[str, Any]:
        self.refresh_due()
        with self.lock:
            return {
                "generated_at": now_ts(),
                "global_refresh_interval": self.store.global_refresh_interval or DEFAULT_GLOBAL_REFRESH,
                "accounts": [self._serialize_account(state) for state in self.states],
                "events": list(reversed(self.event_log[-80:])),
            }


def html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Usage Hub</title>
  <style>
    :root{
      --bg:#0b0e14;
      --bg-2:#111520;
      --surface:#161b26;
      --surface-2:#1c2233;
      --surface-3:#232a3b;
      --text:#e2e8f0;
      --text-2:#94a3b8;
      --text-3:#64748b;
      --border:#1e293b;
      --border-2:#334155;
      --accent:#6366f1;
      --accent-glow:rgba(99,102,241,.25);
      --green:#22c55e;
      --green-dim:rgba(34,197,94,.15);
      --yellow:#eab308;
      --yellow-dim:rgba(234,179,8,.15);
      --red:#ef4444;
      --red-dim:rgba(239,68,68,.15);
      --blue:#3b82f6;
      --blue-dim:rgba(59,130,246,.15);
      --radius:16px;
      --radius-sm:10px;
      --radius-xs:6px;
      --mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
      --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      --shadow:0 4px 24px rgba(0,0,0,.35);
      --transition:all .2s cubic-bezier(.4,0,.2,1);
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5;min-height:100vh;padding-bottom:220px}
    button,input,select,textarea{font:inherit;color:inherit}

    /* Toast */
    .toast-container{position:fixed;top:20px;right:20px;z-index:100;display:flex;flex-direction:column;gap:8px}
    .toast{padding:12px 20px;border-radius:var(--radius-sm);background:var(--surface-2);border:1px solid var(--border-2);color:var(--text);font-size:14px;font-weight:500;box-shadow:var(--shadow);transform:translateX(120%);opacity:0;transition:transform .3s cubic-bezier(.4,0,.2,1),opacity .3s ease}
    .toast.show{transform:translateX(0);opacity:1}
    .toast.success{border-left:3px solid var(--green)}
    .toast.error{border-left:3px solid var(--red)}
    .toast.info{border-left:3px solid var(--blue)}

    /* Nav */
    .nav{position:sticky;top:0;z-index:10;background:rgba(11,14,20,.85);backdrop-filter:blur(20px) saturate(1.2);border-bottom:1px solid var(--border);padding:0 24px}
    .nav-inner{max-width:1520px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:56px;gap:16px}
    .nav-brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;letter-spacing:-.02em}
    .nav-brand svg{width:24px;height:24px}
    .nav-right{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--text-3)}
    .nav-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

    /* Shell */
    .shell{max-width:1520px;margin:0 auto;padding:20px 24px 40px}

    /* Header */
    .header{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:start;padding-bottom:20px;border-bottom:1px solid var(--border)}
    .header-left h1{font-size:28px;font-weight:700;letter-spacing:-.03em;background:linear-gradient(135deg,#e2e8f0,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
    .header-left p{color:var(--text-3);font-size:14px;margin-top:2px}
    .header-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

    /* Buttons */
    .btn{display:inline-flex;align-items:center;gap:6px;border:none;border-radius:var(--radius-sm);padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;transition:var(--transition);white-space:nowrap}
    .btn:hover{transform:translateY(-1px)}
    .btn:active{transform:translateY(0)}
    .btn-primary{background:var(--accent);color:#fff;box-shadow:0 2px 12px var(--accent-glow)}
    .btn-primary:hover{background:#818cf8;box-shadow:0 4px 20px var(--accent-glow)}
    .btn-ghost{background:var(--surface-2);color:var(--text-2);border:1px solid var(--border)}
    .btn-ghost:hover{background:var(--surface-3);color:var(--text);border-color:var(--border-2)}
    .btn-danger{background:var(--red-dim);color:var(--red);border:1px solid rgba(239,68,68,.25)}
    .btn-danger:hover{background:rgba(239,68,68,.25)}
    .btn-success{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,197,94,.25)}
    .btn-sm{padding:6px 12px;font-size:12px}

    /* Metrics strip */
    .metrics{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}
    .metric{display:flex;align-items:center;gap:12px;padding:14px 18px;border-radius:var(--radius);background:var(--surface);border:1px solid var(--border);flex:1;min-width:180px}
    .metric-icon{width:40px;height:40px;border-radius:var(--radius-sm);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
    .metric-icon.accounts{background:var(--blue-dim);color:var(--blue)}
    .metric-icon.healthy{background:var(--green-dim);color:var(--green)}
    .metric-icon.errors{background:var(--red-dim);color:var(--red)}
    .metric-icon.refresh{background:rgba(99,102,241,.12);color:var(--accent)}
    .metric-body .label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-3)}
    .metric-body .value{font-size:22px;font-weight:700;letter-spacing:-.02em}

    /* Settings row */
    .settings-row{display:flex;gap:10px;align-items:end;margin-top:16px;flex-wrap:wrap}
    .settings-row .field{display:flex;flex-direction:column;gap:4px}
    .settings-row .field label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3)}
    .settings-row input,.settings-row select{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-xs);padding:8px 12px;color:var(--text);font-size:13px;min-width:140px}
    .settings-row input:focus,.settings-row select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}

    /* Layout */
    .layout{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px;align-items:start}

    /* Cards section */
    .section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
    .section-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
    .section-head h2{font-size:16px;font-weight:600}
    .section-head .count{font-size:12px;color:var(--text-3);background:var(--surface-2);padding:2px 8px;border-radius:999px}

    .cards{display:flex;flex-direction:column}
    .card{padding:16px 20px;border-bottom:1px solid var(--border);cursor:pointer;transition:var(--transition);position:relative}
    .card:last-child{border-bottom:none}
    .card:hover{background:var(--surface-2)}
    .card.active{background:var(--surface-2);box-shadow:inset 3px 0 0 var(--accent)}
    .card-row{display:flex;justify-content:space-between;align-items:center;gap:12px}
    .card-identity{display:flex;align-items:center;gap:10px;min-width:0}
    .card-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .card-dot.ok{background:var(--green);box-shadow:0 0 6px rgba(34,197,94,.4)}
    .card-dot.warn{background:var(--yellow);box-shadow:0 0 6px rgba(234,179,8,.4)}
    .card-dot.err{background:var(--red);box-shadow:0 0 6px rgba(239,68,68,.4)}
    .card-dot.idle{background:var(--text-3)}
    .card-name{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .card-badge{flex-shrink:0;font-size:11px;padding:3px 8px;border-radius:999px;background:var(--surface-3);color:var(--text-3);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
    .card-snapshot{color:var(--text-2);font-size:13px;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .card-bars{display:flex;gap:8px;margin-top:10px}
    .card-bar-group{flex:1;min-width:0}
    .card-bar-head{display:flex;justify-content:space-between;font-size:11px;color:var(--text-3);margin-bottom:4px}
    .bar{height:6px;border-radius:999px;background:var(--surface-3);overflow:hidden;position:relative}
    .fill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;transition:width .6s cubic-bezier(.4,0,.2,1);background:linear-gradient(90deg,var(--accent),#818cf8)}
    .fill.warn{background:linear-gradient(90deg,#ca8a04,var(--yellow))}
    .fill.danger{background:linear-gradient(90deg,#dc2626,#f87171)}

    /* Detail panel */
    #detail-panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;position:sticky;top:76px}
    .detail-empty{padding:60px 20px;text-align:center;color:var(--text-3)}
    .detail-empty svg{width:48px;height:48px;margin-bottom:12px;opacity:.3}
    .detail-head{padding:20px;border-bottom:1px solid var(--border)}
    .detail-head .eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);font-weight:600}
    .detail-head h2{font-size:22px;font-weight:700;margin-top:4px;letter-spacing:-.02em;word-break:break-word}
    .detail-head .snapshot{color:var(--text-2);font-size:13px;margin-top:4px}
    .detail-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
    .chip{padding:4px 10px;border-radius:999px;font-size:11px;font-weight:600;background:var(--surface-3);color:var(--text-2)}
    .detail-actions{display:flex;gap:6px;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--bg-2)}

    /* Detail windows */
    .detail-windows{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:16px 20px;border-bottom:1px solid var(--border)}
    .window-card{padding:14px;border-radius:var(--radius-sm);background:var(--bg-2);border:1px solid var(--border)}
    .window-card .wlabel{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);margin-bottom:6px}
    .window-card .wvalue{font-size:18px;font-weight:700}
    .window-card .wreset{font-size:11px;color:var(--text-3);margin-top:4px}
    .window-card .bar{height:8px;margin-top:8px}

    /* KV grid */
    .kv{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);border-bottom:1px solid var(--border)}
    .kv-item{padding:12px 16px;background:var(--surface)}
    .kv-item .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3)}
    .kv-item .v{font-size:13px;font-weight:600;margin-top:2px;word-break:break-word;overflow-wrap:anywhere}

    /* Probe table */
    .table-section{border-bottom:1px solid var(--border)}
    .table-section-head{padding:12px 20px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);font-weight:600;background:var(--bg-2)}
    .table-wrap{overflow:auto;max-height:280px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{padding:10px 16px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border)}
    th{background:var(--bg-2);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);font-weight:600;position:sticky;top:0}
    .code{font-family:var(--mono);font-size:12px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;color:var(--text-2)}

    /* Summary JSON */
    .json-block{padding:16px 20px;max-height:240px;overflow:auto}
    .json-block .label{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);font-weight:600;margin-bottom:8px}
    .json-block pre{font-family:var(--mono);font-size:12px;color:var(--text-2);white-space:pre-wrap;word-break:break-word;line-height:1.6;background:var(--bg-2);padding:12px;border-radius:var(--radius-xs);border:1px solid var(--border)}

    /* Empty / muted */
    .muted{color:var(--text-3)}
    .empty{padding:40px 20px;text-align:center;color:var(--text-3)}

    /* Modal */
    .modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;padding:20px;z-index:50}
    .modal-backdrop.open{display:flex}
    .modal{width:min(780px,100%);max-height:90vh;overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 24px 64px rgba(0,0,0,.5)}
    .modal-head{padding:20px 24px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
    .modal-head-text .eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);font-weight:600}
    .modal-head h3{font-size:20px;font-weight:700;margin-top:4px}
    .modal-head p{color:var(--text-3);font-size:13px;margin-top:2px}
    .switcher{display:flex;gap:6px;padding:12px 24px;border-bottom:1px solid var(--border);flex-wrap:wrap;background:var(--bg-2)}
    .switcher .btn{border-radius:999px}
    .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:20px 24px}
    .field{display:flex;flex-direction:column;gap:4px}
    .field.full{grid-column:1 / -1}
    .field label{font-size:12px;color:var(--text-3);font-weight:500}
    .form-grid input,.form-grid select,.form-grid textarea{background:var(--bg-2);border:1px solid var(--border);border-radius:var(--radius-xs);padding:10px 14px;color:var(--text);font-size:13px}
    .form-grid input:focus,.form-grid textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
    textarea{min-height:80px;resize:vertical}
    .modal-footer{padding:16px 24px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
    .modal-footer .note{font-size:12px;color:var(--text-3);max-width:400px}

    /* Live log */
    .live-log{position:fixed;left:24px;right:24px;bottom:16px;z-index:35;background:rgba(17,21,32,.94);border:1px solid var(--border-2);border-radius:var(--radius);box-shadow:var(--shadow);backdrop-filter:blur(18px) saturate(1.1);overflow:hidden}
    .live-log-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);background:rgba(11,14,20,.8)}
    .live-log-head .title{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-2)}
    .live-log-head .status{font-size:12px;color:var(--text-3)}
    .live-log-body{max-height:150px;overflow:auto;padding:10px 12px 12px}
    .log-empty{padding:14px 8px;color:var(--text-3);font-size:13px}
    .log-entry{padding:10px 12px;border-radius:var(--radius-xs);background:var(--surface);border:1px solid var(--border);margin-bottom:8px}
    .log-entry:last-child{margin-bottom:0}
    .log-entry.success{border-left:3px solid var(--green)}
    .log-entry.error{border-left:3px solid var(--red)}
    .log-entry.info{border-left:3px solid var(--blue)}
    .log-meta{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em}
    .log-message{margin-top:4px;font-size:13px;color:var(--text);word-break:break-word;overflow-wrap:anywhere}

    /* Scrollbar */
    ::-webkit-scrollbar{width:6px;height:6px}
    ::-webkit-scrollbar-track{background:transparent}
    ::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:3px}
    ::-webkit-scrollbar-thumb:hover{background:var(--text-3)}

    /* Responsive */
    @media(max-width:1100px){
      .layout{grid-template-columns:1fr}
      #detail-panel{position:static}
      .header{grid-template-columns:1fr}
      .header-actions{justify-content:flex-start}
    }
    @media(max-width:640px){
      .shell{padding:12px}
      .metrics{flex-direction:column}
      .detail-windows,.kv,.form-grid{grid-template-columns:1fr}
      .settings-row{flex-direction:column;align-items:stretch}
      .settings-row input,.settings-row select{min-width:0;width:100%}
      .nav-inner{height:48px}
      .live-log{left:12px;right:12px;bottom:12px}
      .live-log-body{max-height:170px}
    }

    /* Skeleton loading */
    @keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
    .skeleton{background:linear-gradient(90deg,var(--surface-2) 25%,var(--surface-3) 50%,var(--surface-2) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:var(--radius-xs)}
  </style>
</head>
<body>
  <nav class="nav">
    <div class="nav-inner">
      <div class="nav-brand">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg>
        Usage Hub
      </div>
      <div class="nav-right">
        <span class="nav-dot"></span>
        <span id="nav-status">Connecting...</span>
      </div>
    </div>
  </nav>

  <div class="toast-container" id="toast-container"></div>

  <div class="shell">
    <div class="header">
      <div class="header-left">
        <h1>Claude &amp; Codex Dashboard</h1>
        <p>Live usage monitoring, OAuth discovery, and account management</p>
      </div>
      <div class="header-actions">
        <button class="btn btn-primary" id="refresh-all">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Refresh All
        </button>
        <button class="btn btn-success" id="discover">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          Discover OAuth
        </button>
        <button class="btn btn-ghost" id="add-account">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Add Account
        </button>
      </div>
    </div>

    <div class="metrics">
      <div class="metric">
        <div class="metric-icon accounts">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        </div>
        <div class="metric-body"><div class="label">Accounts</div><div class="value" id="metric-accounts">-</div></div>
      </div>
      <div class="metric">
        <div class="metric-icon healthy">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        </div>
        <div class="metric-body"><div class="label">Healthy</div><div class="value" id="metric-ok">-</div></div>
      </div>
      <div class="metric">
        <div class="metric-icon errors">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        </div>
        <div class="metric-body"><div class="label">Errors</div><div class="value" id="metric-errors">-</div></div>
      </div>
      <div class="metric">
        <div class="metric-icon refresh">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </div>
        <div class="metric-body"><div class="label">Last Update</div><div class="value" id="metric-time" style="font-size:14px">-</div></div>
      </div>
    </div>

    <div class="settings-row">
      <div class="field">
        <label for="global-refresh">Refresh interval (s)</label>
        <input id="global-refresh" type="number" min="5" max="600" step="5" value="30">
      </div>
      <button class="btn btn-ghost btn-sm" id="save-refresh" style="margin-bottom:1px">Save</button>
      <div class="field">
        <label for="polling">Browser polling</label>
        <select id="polling">
          <option value="5000">Every 5s</option>
          <option value="10000" selected>Every 10s</option>
          <option value="30000">Every 30s</option>
          <option value="0">Manual only</option>
        </select>
      </div>
    </div>

    <section class="layout">
      <div class="section">
        <div class="section-head">
          <h2>Accounts</h2>
          <span class="count" id="card-count">0</span>
        </div>
        <div class="cards" id="cards">
          <div class="empty">No accounts yet. Click "Add Account" or "Discover OAuth" to get started.</div>
        </div>
      </div>

      <div id="detail-panel">
        <div class="detail-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
          <div>Select an account to view details</div>
        </div>
      </div>
    </section>
  </div>

  <div class="modal-backdrop" id="modal-backdrop">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-head-text">
          <div class="eyebrow">Account Setup</div>
          <h3 id="modal-title">Add Account</h3>
          <p id="modal-subtitle">Create a Claude or Codex/OpenAI account entry.</p>
        </div>
        <button class="btn btn-ghost btn-sm" id="close-modal">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          Close
        </button>
      </div>

      <div class="switcher">
        <button class="btn btn-primary btn-sm" data-mode="claude-api">Claude API</button>
        <button class="btn btn-ghost btn-sm" data-mode="codex-api">OpenAI API</button>
        <button class="btn btn-ghost btn-sm" data-mode="claude-oauth">Claude OAuth</button>
        <button class="btn btn-ghost btn-sm" data-mode="codex-oauth">Codex OAuth</button>
        <button class="btn btn-ghost btn-sm" data-mode="keychain-browse" style="border-color:var(--accent);">Browse Keychain</button>
      </div>

      <form id="account-form">
        <div class="form-grid" id="form-grid"></div>
        <div class="modal-footer">
          <div class="note">API keys are stored locally in the git-ignored config file.</div>
          <button class="btn btn-primary" type="submit">Save Account</button>
        </div>
      </form>
    </div>
  </div>

  <section class="live-log" aria-label="Live load log">
    <div class="live-log-head">
      <div class="title">Live Load Log</div>
      <div class="status" id="live-log-status">Waiting for events...</div>
    </div>
    <div class="live-log-body" id="live-log-body">
      <div class="log-empty">No load or refresh events yet.</div>
    </div>
  </section>

  <script>
    const state = {
      snapshot: null,
      selectedId: null,
      mode: 'claude-api',
      pollingTimer: null,
      clientEvents: [],
      loadOk: null,
    };

    const formModes = {
      'claude-api': {
        title: 'Add Claude API account',
        subtitle: 'Probe Anthropic usage via API key.',
        fields: [
          ['name', 'Account label', 'text', 'Claude API'],
          ['email', 'Email / Gmail', 'email', ''],
          ['api_key', 'API key', 'password', ''],
          ['models', 'Models (comma separated)', 'textarea', 'claude-opus-4-6, claude-haiku-4-5-20251001'],
          ['api_base', 'API base', 'text', 'https://api.anthropic.com'],
          ['refresh_interval', 'Refresh seconds', 'number', ''],
        ],
        payload: { provider: 'claude', auth_kind: 'api' },
      },
      'codex-api': {
        title: 'Add Codex/OpenAI API account',
        subtitle: 'Probe OpenAI usage and rate limits via API key.',
        fields: [
          ['name', 'Account label', 'text', 'OpenAI API'],
          ['email', 'Email / Gmail', 'email', ''],
          ['api_key', 'API key', 'password', ''],
          ['models', 'Models (comma separated)', 'textarea', 'gpt-4.1-mini, gpt-4.1'],
          ['api_base', 'API base', 'text', 'https://api.openai.com'],
          ['refresh_interval', 'Refresh seconds', 'number', ''],
        ],
        payload: { provider: 'codex', auth_kind: 'api' },
      },
      'claude-oauth': {
        title: 'Add Claude OAuth connection',
        subtitle: 'Point the dashboard at a Claude Code keychain entry.',
        fields: [
          ['name', 'Connection label', 'text', 'Claude OAuth'],
          ['email', 'Email / Gmail', 'email', ''],
          ['keychain_service', 'Keychain service', 'text', 'Claude Code-credentials'],
          ['keychain_account', 'Keychain account', 'text', ''],
          ['client_id', 'OAuth client ID', 'text', ''],
          ['models', 'Models (comma separated)', 'textarea', 'claude-opus-4-6, claude-haiku-4-5-20251001'],
          ['refresh_interval', 'Refresh seconds', 'number', ''],
        ],
        payload: { provider: 'claude', auth_kind: 'oauth' },
      },
      'codex-oauth': {
        title: 'Add Codex OAuth connection',
        subtitle: 'Point the dashboard at a Codex auth.json and sessions directory.',
        fields: [
          ['name', 'Connection label', 'text', 'Codex OAuth'],
          ['email', 'Email / Gmail', 'email', ''],
          ['auth_file', 'auth.json path', 'text', '~/.codex/auth.json'],
          ['sessions_dir', 'sessions directory', 'text', '~/.codex/sessions'],
          ['client_id', 'OAuth client ID', 'text', 'app_EMoamEEZ73f0CkXaXp7hrann'],
          ['refresh_interval', 'Refresh seconds', 'number', ''],
        ],
        payload: { provider: 'codex', auth_kind: 'oauth' },
      },
    };

    function el(id){ return document.getElementById(id); }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function flash(message, timeout = 3000, type = 'info') {
      const container = el('toast-container');
      const toast = document.createElement('div');
      toast.className = `toast ${type}`;
      toast.textContent = message;
      container.appendChild(toast);
      requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));
      setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
      }, timeout);
    }

    function formatDate(epoch) {
      if (!epoch) return '-';
      return new Date(epoch * 1000).toLocaleString();
    }

    function formatTime(epoch) {
      if (!epoch) return '-';
      return new Date(epoch * 1000).toLocaleTimeString();
    }

    function addClientEvent(message, type = 'info', source = 'browser') {
      state.clientEvents.unshift({
        at: Date.now() / 1000,
        type,
        message,
        source,
      });
      state.clientEvents = state.clientEvents.slice(0, 40);
    }

    function mergedEvents() {
      const serverEvents = state.snapshot?.events || [];
      return [...state.clientEvents, ...serverEvents]
        .sort((a, b) => (b.at || 0) - (a.at || 0))
        .slice(0, 80);
    }

    function renderEventLog() {
      const body = el('live-log-body');
      const status = el('live-log-status');
      const events = mergedEvents();
      status.textContent = state.loadOk === false ? 'Latest browser load failed' : state.loadOk === true ? 'Streaming pass/fail results' : 'Waiting for events...';
      if (!events.length) {
        body.innerHTML = '<div class="log-empty">No load or refresh events yet.</div>';
        return;
      }
      body.innerHTML = events.map((event) => {
        const tone = event.type || 'info';
        const when = formatDate(event.at);
        const who = event.account_name || event.source || 'event';
        return `
          <div class="log-entry ${escapeHtml(tone)}">
            <div class="log-meta">
              <span>${escapeHtml(who)}</span>
              <span>${escapeHtml(when)}</span>
            </div>
            <div class="log-message">${escapeHtml(event.message || '-')}</div>
          </div>
        `;
      }).join('');
    }

    function fillClass(percent) {
      if (percent == null) return '';
      if (percent >= 100) return 'danger';
      if (percent >= 75) return 'warn';
      return '';
    }

    function dotClass(account) {
      if (account.error) return 'err';
      if (!account.last_success_at) return 'idle';
      const w5 = account.windows['5h'].percent;
      const w7 = account.windows['7d'].percent;
      if ((w5 != null && w5 >= 90) || (w7 != null && w7 >= 90)) return 'warn';
      return 'ok';
    }

    function renderCards() {
      const cards = el('cards');
      const accounts = state.snapshot?.accounts || [];
      el('card-count').textContent = String(accounts.length);
      cards.innerHTML = '';
      if (!accounts.length) {
        cards.innerHTML = '<div class="empty">No accounts yet. Click "Add Account" or "Discover OAuth" to get started.</div>';
        return;
      }
      accounts.forEach((account) => {
        const card = document.createElement('div');
        card.className = 'card' + (account.id === state.selectedId ? ' active' : '');
        const p5 = Math.max(0, Math.min(100, account.windows['5h'].percent || 0));
        const p7 = Math.max(0, Math.min(100, account.windows['7d'].percent || 0));
        card.innerHTML = `
          <div class="card-row">
            <div class="card-identity">
              <div class="card-dot ${dotClass(account)}"></div>
              <div>
                <span class="card-name">${account.record.name}</span>
                ${account.record.email ? `<div style="font-size:0.7rem;color:var(--muted);margin-top:1px;">${account.record.email}</div>` : ''}
              </div>
            </div>
            <span class="card-badge">${account.record.provider} ${account.record.auth_kind}</span>
          </div>
          <div class="card-snapshot">${account.snapshot || 'Waiting for refresh...'}</div>
          <div class="card-bars">
            <div class="card-bar-group">
              <div class="card-bar-head"><span>5h</span><span>${account.windows['5h'].text}</span></div>
              <div class="bar"><div class="fill ${fillClass(account.windows['5h'].percent)}" style="width:${p5}%"></div></div>
            </div>
            <div class="card-bar-group">
              <div class="card-bar-head"><span>7d</span><span>${account.windows['7d'].text}</span></div>
              <div class="bar"><div class="fill ${fillClass(account.windows['7d'].percent)}" style="width:${p7}%"></div></div>
            </div>
          </div>
        `;
        card.addEventListener('click', () => {
          state.selectedId = account.id;
          render();
        });
        cards.appendChild(card);
      });
    }

    function kvItem(key, value) {
      return `<div class="kv-item"><div class="k">${key}</div><div class="v">${value ?? '-'}</div></div>`;
    }

    function probeRows(account) {
      const entries = Object.values(account.models || {});
      if (!entries.length) {
        return '<tr><td colspan="5" class="muted">No probe data yet.</td></tr>';
      }
      return entries.map((probe) => `
        <tr>
          <td class="code">${probe.model}</td>
          <td>${probe.status || '-'}</td>
          <td class="code">${JSON.stringify(probe.response_usage || {})}</td>
          <td class="code">${JSON.stringify(probe.rate_limits || {})}</td>
          <td>${probe.error || '-'}</td>
        </tr>
      `).join('');
    }

    function renderDetail() {
      const panel = el('detail-panel');
      const account = (state.snapshot?.accounts || []).find((item) => item.id === state.selectedId);
      if (!account) {
        panel.innerHTML = '<div class="detail-empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg><div>Select an account to view details</div></div>';
        return;
      }
      const summary = account.summary || {};
      const p5 = Math.max(0, Math.min(100, account.windows['5h'].percent || 0));
      const p7 = Math.max(0, Math.min(100, account.windows['7d'].percent || 0));
      panel.innerHTML = `
        <div class="detail-head">
          <div class="eyebrow">${account.record.provider} / ${account.record.auth_kind}</div>
          <h2>${account.record.name}</h2>
          ${account.record.email
            ? `<div data-action="set-email" style="font-size:0.8rem;color:var(--muted);margin-top:-0.25rem;margin-bottom:0.25rem;cursor:pointer;" title="Click to edit email">${account.record.email}</div>`
            : `<button class="btn btn-ghost btn-sm" data-action="set-email" style="font-size:0.7rem;padding:2px 8px;margin-top:-0.15rem;margin-bottom:0.25rem;">+ Add email</button>`}
          <div class="snapshot">${account.snapshot || 'Waiting for data'}</div>
          <div class="detail-chips">
            <span class="chip">Next: ${account.next_refresh_in}</span>
            <span class="chip">Last OK: ${account.last_success_relative}</span>
            <span class="chip">Source: ${account.record.source}</span>
          </div>
        </div>

        <div class="detail-actions">
          <button class="btn btn-primary btn-sm" data-action="refresh">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
          <button class="btn btn-ghost btn-sm" data-action="toggle-visible">${account.record.visible ? 'Hide' : 'Show'}</button>
          <button class="btn btn-ghost btn-sm" data-action="toggle-enabled">${account.record.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-danger btn-sm" data-action="delete">Delete</button>
        </div>

        <div class="detail-windows">
          <div class="window-card">
            <div class="wlabel">5-Hour Window</div>
            <div class="wvalue">${account.windows['5h'].text}</div>
            <div class="bar"><div class="fill ${fillClass(account.windows['5h'].percent)}" style="width:${p5}%"></div></div>
            <div class="wreset">Resets: ${account.windows['5h'].reset}</div>
          </div>
          <div class="window-card">
            <div class="wlabel">7-Day Window</div>
            <div class="wvalue">${account.windows['7d'].text}</div>
            <div class="bar"><div class="fill ${fillClass(account.windows['7d'].percent)}" style="width:${p7}%"></div></div>
            <div class="wreset">Resets: ${account.windows['7d'].reset}</div>
          </div>
        </div>

        <div class="kv">
          ${kvItem('Email', account.record.email || '-')}
          ${kvItem('Last refreshed', formatDate(account.last_refresh_finished_at))}
          ${kvItem('Status', account.error || account.status_text)}
          ${kvItem('Default model', account.record.default_model || '-')}
          ${kvItem('Refresh interval', account.record.refresh_interval || state.snapshot.global_refresh_interval)}
          ${kvItem('API base', account.record.api_base || '-')}
          ${kvItem('API key', account.record.api_key_masked || '-')}
          ${kvItem('Auth file', account.record.auth_file || '-')}
          ${kvItem('Sessions dir', account.record.sessions_dir || '-')}
          ${kvItem('Keychain service', account.record.keychain_service || '-')}
          ${kvItem('Keychain account', account.record.keychain_account || '-')}
        </div>

        <div class="table-section">
          <div class="table-section-head">Model Probes</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>Model</th><th>Status</th><th>Usage</th><th>Rate Limits</th><th>Error</th></tr>
              </thead>
              <tbody>${probeRows(account)}</tbody>
            </table>
          </div>
        </div>

        <div class="json-block">
          <div class="label">Summary JSON</div>
          <pre>${JSON.stringify(summary, null, 2)}</pre>
        </div>
      `;
      panel.querySelectorAll('[data-action]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const action = btn.dataset.action;
          if (action === 'refresh') {
            await api(`/api/accounts/${account.id}/refresh`, { method: 'POST' });
            await loadSnapshot(true);
            flash(`Refreshed ${account.record.name}`, 2500, 'success');
          } else if (action === 'toggle-visible') {
            await api(`/api/accounts/${account.id}`, { method: 'PATCH', body: { visible: !account.record.visible } });
            await loadSnapshot(true);
          } else if (action === 'toggle-enabled') {
            await api(`/api/accounts/${account.id}`, { method: 'PATCH', body: { enabled: !account.record.enabled } });
            await loadSnapshot(true);
          } else if (action === 'set-email') {
            const email = window.prompt('Email / Gmail for this account:', account.record.email || '');
            if (email === null) return;
            await api(`/api/accounts/${account.id}`, { method: 'PATCH', body: { email: email.trim() } });
            await loadSnapshot(true);
            flash(email.trim() ? 'Email saved' : 'Email cleared', 2500, 'success');
          } else if (action === 'delete') {
            const warn = account.record.user_added ? '' : '\\n(Discovered account — may reappear on next discovery)';
            if (!window.confirm(`Delete ${account.record.name}?${warn}`)) return;
            await api(`/api/accounts/${account.id}`, { method: 'DELETE' });
            if (state.selectedId === account.id) state.selectedId = null;
            await loadSnapshot(true);
            flash(`Deleted ${account.record.name}`, 2500, 'success');
          }
        });
      });
    }

    function renderMetrics() {
      const accounts = state.snapshot?.accounts || [];
      const ok = accounts.filter((a) => !a.error).length;
      const errors = accounts.filter((a) => a.error).length;
      el('metric-accounts').textContent = String(accounts.length);
      el('metric-ok').textContent = String(ok);
      el('metric-errors').textContent = String(errors);
      el('metric-time').textContent = state.snapshot ? formatTime(state.snapshot.generated_at) : '-';
      el('nav-status').textContent = state.snapshot ? `Live` : 'Connecting...';
      el('global-refresh').value = state.snapshot?.global_refresh_interval || 30;
    }

    function render() {
      renderMetrics();
      renderCards();
      renderDetail();
      renderEventLog();
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        method: options.method || 'GET',
        headers: { 'Content-Type': 'application/json' },
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `Request failed with ${response.status}`);
      }
      return data;
    }

    async function loadSnapshot(keepSelection = false) {
      try {
        const data = await api('/api/state');
        state.snapshot = data;
        if (!keepSelection) {
          if (!state.selectedId || !data.accounts.find((a) => a.id === state.selectedId)) {
            state.selectedId = data.accounts[0]?.id || null;
          }
        } else if (!data.accounts.find((a) => a.id === state.selectedId)) {
          state.selectedId = data.accounts[0]?.id || null;
        }
        if (state.loadOk !== true) {
          addClientEvent(`Snapshot load passed: ${data.accounts?.length || 0} account(s) available.`, 'success');
        }
        state.loadOk = true;
        render();
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        if (state.loadOk !== false) {
          addClientEvent(`Snapshot load failed: ${message}`, 'error');
        }
        state.loadOk = false;
        renderEventLog();
        throw err;
      }
    }

    function openModal(mode = 'claude-api') {
      state.mode = mode;
      renderForm();
      el('modal-backdrop').classList.add('open');
    }

    function closeModal() {
      el('modal-backdrop').classList.remove('open');
    }

    function renderForm() {
      document.querySelectorAll('[data-mode]').forEach((btn) => {
        const cls = btn.dataset.mode === state.mode ? 'btn btn-primary btn-sm' : 'btn btn-ghost btn-sm';
        btn.className = btn.dataset.mode === 'keychain-browse' ? cls + ' ' : cls;
        if (btn.dataset.mode === 'keychain-browse') btn.style.borderColor = 'var(--accent)';
      });
      const grid = el('form-grid');
      const footer = document.querySelector('.modal-footer');

      if (state.mode === 'keychain-browse') {
        el('modal-title').textContent = 'Browse Keychain';
        el('modal-subtitle').textContent = 'Scanning macOS keychain for Claude & Codex OAuth entries...';
        grid.innerHTML = '<div class="muted" style="padding:1rem;text-align:center;">Scanning...</div>';
        if (footer) footer.style.display = 'none';
        scanKeychain();
        return;
      }

      if (footer) footer.style.display = '';
      const cfg = formModes[state.mode];
      el('modal-title').textContent = cfg.title;
      el('modal-subtitle').textContent = cfg.subtitle;
      grid.innerHTML = '';
      cfg.fields.forEach(([name, label, type, placeholder]) => {
        const field = document.createElement('div');
        field.className = 'field' + (type === 'textarea' ? ' full' : '');
        const input = type === 'textarea'
          ? `<textarea id="field-${name}" placeholder="${placeholder}"></textarea>`
          : `<input id="field-${name}" type="${type}" placeholder="${placeholder}" value="${type !== 'password' ? placeholder : ''}">`;
        field.innerHTML = `<label for="field-${name}">${label}</label>${input}`;
        grid.appendChild(field);
      });
    }

    async function scanKeychain() {
      try {
        const data = await api('/api/keychain/scan');
        const grid = el('form-grid');
        const entries = data.entries || [];
        const clientId = data.client_id || '';
        const cmd = data.command || '';
        const oauthCount = entries.filter(e => e.has_oauth).length;
        el('modal-subtitle').textContent = entries.length
          ? `Found ${entries.length} keychain entry(s), ${oauthCount} with valid OAuth.`
          : 'No Claude/Codex entries found in keychain.';
        grid.innerHTML = '';

        // Show the scan command
        const cmdBlock = document.createElement('div');
        cmdBlock.style.cssText = 'background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:0.5rem 0.75rem;margin-bottom:0.75rem;font-family:monospace;font-size:0.7rem;color:var(--muted);word-break:break-all;white-space:pre-wrap;line-height:1.5;';
        cmdBlock.innerHTML = `<span style="color:var(--green);">$</span> ${escapeHtml(cmd)}`;
        grid.appendChild(cmdBlock);

        if (!entries.length) {
          const empty = document.createElement('div');
          empty.className = 'muted';
          empty.style.cssText = 'padding:1.5rem;text-align:center;';
          empty.textContent = 'No Claude or Codex OAuth entries found in keychain.';
          grid.appendChild(empty);
          return;
        }

        entries.forEach((entry, idx) => {
          const card = document.createElement('div');
          card.style.cssText = 'padding:0.75rem 1rem;border:1px solid var(--border);border-radius:8px;transition:border-color .15s;margin-bottom:0.75rem;';

          const providerLabel = entry.provider === 'codex' ? 'Codex' : 'Claude';
          const badge = entry.has_oauth
            ? `<span style="color:var(--green);font-size:0.75rem;">Valid OAuth</span>`
            : `<span style="color:var(--muted);font-size:0.75rem;">No OAuth data</span>`;
          const sub = entry.subscription_type
            ? `<span style="color:var(--accent);font-size:0.75rem;margin-left:0.5rem;">${entry.subscription_type}</span>`
            : '';
          const providerBadge = entry.has_oauth
            ? `<span style="font-size:0.7rem;color:var(--muted);background:var(--bg);padding:1px 6px;border-radius:3px;margin-left:0.5rem;">${providerLabel}</span>`
            : '';
          const token = entry.token_preview
            ? `<div style="font-size:0.7rem;color:var(--muted);font-family:monospace;margin-top:0.25rem;">${entry.token_preview}</div>`
            : '';

          // Per-entry security command
          const entryCmd = entry.command || '';
          const cmdLine = entryCmd
            ? `<div style="font-family:monospace;font-size:0.65rem;color:var(--muted);margin-top:0.35rem;word-break:break-all;white-space:pre-wrap;background:var(--bg);padding:3px 6px;border-radius:4px;"><span style="color:var(--green);">$</span> ${escapeHtml(entryCmd)}</div>`
            : '';

          const nameId = `kc-name-${idx}`;
          const emailId = `kc-email-${idx}`;
          const defaultName = entry.service === 'Claude Code-credentials' ? 'Claude OAuth'
            : entry.service.startsWith('Claude Code-credentials') ? 'Claude OAuth ' + entry.service.replace('Claude Code-credentials', '').replace('-','').trim()
            : entry.service;

          const inputRow = entry.has_oauth ? `
            <div style="display:flex;gap:0.5rem;margin-top:0.5rem;flex-wrap:wrap;">
              <input id="${nameId}" type="text" placeholder="Account label" value="${escapeHtml(defaultName)}"
                style="flex:1 1 140px;min-width:120px;font-size:0.75rem;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);">
              <input id="${emailId}" type="email" placeholder="Email / Gmail"
                style="flex:1 1 160px;min-width:120px;font-size:0.75rem;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);">
              <button class="btn btn-primary btn-sm kc-add" data-idx="${idx}" style="font-size:0.7rem;padding:4px 12px;white-space:nowrap;">Add</button>
            </div>
          ` : '';

          card.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:0.5rem;">
              <div style="min-width:0;flex:1;">
                <div style="font-weight:600;font-size:0.85rem;word-break:break-all;">${escapeHtml(entry.service)}</div>
                <div style="font-size:0.75rem;color:var(--muted);">account: ${escapeHtml(entry.account)}</div>
              </div>
              <div style="text-align:right;flex-shrink:0;">${badge}${sub}${providerBadge}</div>
            </div>
            ${token}
            ${cmdLine}
            ${inputRow}
          `;

          if (!entry.has_oauth) {
            card.style.opacity = '0.5';
          }

          grid.appendChild(card);
        });

        // Wire up the Add buttons
        grid.querySelectorAll('.kc-add').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const idx = Number(btn.dataset.idx);
            const entry = entries[idx];
            const provider = entry.provider || 'claude';
            const name = document.getElementById(`kc-name-${idx}`)?.value?.trim() || entry.service;
            const email = document.getElementById(`kc-email-${idx}`)?.value?.trim() || '';
            try {
              const body = {
                provider, auth_kind: 'oauth',
                name, email: email || undefined,
                keychain_service: entry.service,
                keychain_account: entry.account,
                client_id: clientId,
              };
              await api('/api/accounts', { method: 'POST', body });
              closeModal();
              await loadSnapshot(true);
              flash(`Added ${name}`, 2500, 'success');
            } catch (err) {
              flash(err.message, 4000, 'error');
            }
          });
        });
      } catch (err) {
        el('form-grid').innerHTML = '<div style="padding:1rem;color:var(--red);">Error scanning keychain: ' + escapeHtml(err.message) + '</div>';
      }
    }

    function collectFormPayload() {
      const cfg = formModes[state.mode];
      if (!cfg) return {};
      const payload = { ...cfg.payload };
      cfg.fields.forEach(([name]) => {
        const node = el(`field-${name}`);
        if (!node) return;
        payload[name] = node.value;
      });
      return payload;
    }

    function applyPolling() {
      if (state.pollingTimer) window.clearInterval(state.pollingTimer);
      const ms = Number(el('polling').value || 0);
      if (ms > 0) {
        state.pollingTimer = window.setInterval(() => loadSnapshot(true).catch((err) => flash(err.message, 4000, 'error')), ms);
      }
    }

    async function init() {
      document.querySelectorAll('[data-mode]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.mode = btn.dataset.mode;
          renderForm();
        });
      });
      el('add-account').addEventListener('click', () => openModal('claude-api'));
      el('close-modal').addEventListener('click', closeModal);
      el('modal-backdrop').addEventListener('click', (event) => {
        if (event.target === el('modal-backdrop')) closeModal();
      });
      el('account-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          const payload = collectFormPayload();
          await api('/api/accounts', { method: 'POST', body: payload });
          closeModal();
          await loadSnapshot(true);
          flash('Account saved', 2500, 'success');
        } catch (err) {
          flash(err.message, 4000, 'error');
        }
      });
      el('refresh-all').addEventListener('click', async () => {
        await api('/api/refresh', { method: 'POST' });
        await loadSnapshot(true);
        flash('Refreshed all accounts', 2500, 'success');
      });
      el('discover').addEventListener('click', async () => {
        const data = await api('/api/discover', { method: 'POST' });
        await loadSnapshot(true);
        flash(data.added?.length ? `Discovered ${data.added.length} new account(s)` : 'No new local OAuth accounts found', 3000, data.added?.length ? 'success' : 'info');
      });
      el('save-refresh').addEventListener('click', async () => {
        const seconds = Number(el('global-refresh').value || 30);
        await api('/api/settings', { method: 'PATCH', body: { global_refresh_interval: seconds } });
        await loadSnapshot(true);
        flash(`Global refresh saved at ${seconds}s`, 2500, 'success');
      });
      el('polling').addEventListener('change', applyPolling);
      renderForm();
      applyPolling();
      await loadSnapshot();
    }

    init().catch((err) => flash(err.message, 6000, 'error'));
  </script>
</body>
</html>
"""


class UsageHubHandler(BaseHTTPRequestHandler):
    server: "UsageHubServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(html_page())
            return
        if parsed.path == "/api/state":
            self._send_json(self.server.app.snapshot())
            return
        if parsed.path == "/api/keychain/scan":
            try:
                client_id = discover_claude_client_id() or ""
                entries = scan_keychain_entries()
                self._send_json({
                    "entries": entries,
                    "client_id": client_id,
                    "command": "security dump-keychain | grep -i 'claude\\|codex\\|anthropic\\|openai'\n# then per entry: security find-generic-password -s <service> -a <account> -w",
                })
            except Exception as exc:
                self._error(500, compact_error(exc))
            return
        self._error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/refresh":
                self.server.app.refresh_all()
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/discover":
                added = self.server.app.discover_now()
                self._send_json({"ok": True, "added": added})
                return
            if parsed.path == "/api/accounts":
                payload = json_body(self)
                record = self.server.app.add_account(payload)
                self._send_json({"ok": True, "account_id": record.id}, status=201)
                return
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/refresh"):
                account_id = parsed.path[len("/api/accounts/") : -len("/refresh")].strip("/")
                self.server.app.refresh_account(account_id)
                self._send_json({"ok": True})
                return
        except KeyError as exc:
            self._error(404, str(exc))
            return
        except Exception as exc:
            self._error(400, compact_error(exc))
            return
        self._error(404, "Not found")

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            try:
                payload = json_body(self)
                value = float(payload.get("global_refresh_interval") or DEFAULT_GLOBAL_REFRESH)
                new_value = self.server.app.set_global_refresh(value)
                self._send_json({"ok": True, "global_refresh_interval": new_value})
            except Exception as exc:
                self._error(400, compact_error(exc))
            return
        if parsed.path.startswith("/api/accounts/"):
            account_id = parsed.path[len("/api/accounts/") :].strip("/")
            try:
                payload = json_body(self)
                record = self.server.app.update_account(account_id, payload)
                self._send_json({"ok": True, "account_id": record.id})
            except KeyError as exc:
                self._error(404, str(exc))
            except Exception as exc:
                self._error(400, compact_error(exc))
            return
        self._error(404, "Not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/accounts/"):
            account_id = parsed.path[len("/api/accounts/") :].strip("/")
            try:
                self.server.app.delete_account(account_id)
                self._send_json({"ok": True})
            except KeyError as exc:
                self._error(404, str(exc))
            except Exception as exc:
                self._error(400, compact_error(exc))
            return
        self._error(404, "Not found")


class UsageHubServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app: DashboardApp):
        super().__init__(server_address, UsageHubHandler)
        self.app = app


def main() -> int:
    args = parse_args()
    app = DashboardApp(args.config)
    if not args.no_initial_refresh:
        try:
            app.refresh_all()
        except Exception:
            # Individual account failures are captured inside refresh_state.
            pass

    server = UsageHubServer((args.host, args.port), app)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Usage Hub Web listening at {url}", flush=True)
    print(f"Config: {args.config.expanduser()}", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
