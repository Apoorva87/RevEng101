#!/usr/bin/env python3
"""Shared account, auth, and refresh logic for the usage dashboards."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = PROJECT_DIR / ".local"
CONFIG_PATH = LOCAL_DIR / "usage_hub.json"

DEFAULT_GLOBAL_REFRESH = 30.0
DEFAULT_TIMEOUT = 20.0

CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth0.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_API_BASE = "https://api.anthropic.com"
CLAUDE_BETA_HEADER = "claude-code-20250219,oauth-2025-04-20"
CLAUDE_CODE_SYSTEM_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."
CLAUDE_SCOPES = "user:inference user:inference:claude_code user:sessions:claude_code user:mcp_servers user:file_upload"

OPENAI_API_BASE = "https://api.openai.com"

DEFAULT_OPENAI_MODELS = [
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-5-mini",
]
DEFAULT_CLAUDE_MODELS = [
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-20250514",
    "claude-opus-4-5-20251101",
]


@dataclass
class AccountRecord:
    id: str
    name: str
    provider: str
    auth_kind: str
    enabled: bool = True
    visible: bool = True
    refresh_interval: Optional[float] = None
    default_model: Optional[str] = None
    models: list[str] = field(default_factory=list)
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    auth_file: Optional[str] = None
    sessions_dir: Optional[str] = None
    keychain_service: Optional[str] = None
    keychain_account: Optional[str] = None
    token_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    email: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "saved"
    user_added: bool = True

    def provider_label(self) -> str:
        if self.provider == "codex" and self.auth_kind == "api":
            return "openai"
        return self.provider

    def active_models(self) -> list[str]:
        if self.provider == "codex" and self.auth_kind == "oauth":
            return ["account-usage"]
        models = [model for model in self.models if model.strip()]
        if not models and self.default_model:
            models = [self.default_model]
        return models or (DEFAULT_OPENAI_MODELS[:1] if self.provider == "codex" else DEFAULT_CLAUDE_MODELS[:1])

    def effective_refresh_interval(self, global_interval: float) -> float:
        return float(self.refresh_interval or global_interval)


@dataclass
class ModelProbe:
    model: str
    ok: bool = False
    status: str = "waiting"
    checked_at: Optional[float] = None
    response_usage: dict[str, Any] = field(default_factory=dict)
    rate_limits: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class AccountState:
    record: AccountRecord
    summary: dict[str, Any] = field(default_factory=dict)
    models: dict[str, ModelProbe] = field(default_factory=dict)
    available_models: list[str] = field(default_factory=list)
    error: Optional[str] = None
    last_refresh_started_at: Optional[float] = None
    last_refresh_finished_at: Optional[float] = None
    last_success_at: Optional[float] = None
    next_refresh_at: float = 0.0

    def active_model_name(self) -> str:
        current = self.record.default_model or ""
        if current and current in self.models:
            return current
        if self.record.active_models():
            return self.record.active_models()[0]
        return "-"

    def active_probe(self) -> Optional[ModelProbe]:
        model = self.active_model_name()
        return self.models.get(model)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.global_refresh_interval = DEFAULT_GLOBAL_REFRESH
        self.startup_prompt = True
        self.accounts: list[AccountRecord] = []

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        self.global_refresh_interval = float(data.get("global_refresh_interval") or DEFAULT_GLOBAL_REFRESH)
        self.startup_prompt = bool(data.get("startup_prompt", True))
        self.accounts = [AccountRecord(**item) for item in data.get("accounts", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "global_refresh_interval": self.global_refresh_interval,
            "startup_prompt": self.startup_prompt,
            "accounts": [asdict(account) for account in self.accounts],
        }
        self.path.write_text(json.dumps(payload, indent=2) + os.linesep)


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def format_relative(epoch_seconds: Optional[float]) -> str:
    if not epoch_seconds:
        return "-"
    delta = max(0, int(now_ts() - epoch_seconds))
    if delta < 60:
        return f"{delta}s ago"
    minutes, seconds = divmod(delta, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def format_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours:02d}h"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def coerce_percent(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        if value.endswith("%"):
            value = value[:-1]
        try:
            return float(value)
        except ValueError:
            return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def format_percent_text(raw: Any) -> str:
    percent = coerce_percent(raw)
    if percent is None:
        return "-%"
    return f"{percent:.0f}%"


def format_future_from_remaining(base_epoch: Optional[float], seconds: Any) -> str:
    try:
        if base_epoch is None:
            return "-"
        when = float(base_epoch) + float(seconds)
        return datetime.fromtimestamp(when, timezone.utc).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def format_epoch_like(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def normalize_headers(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def compact_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ").strip() or exc.__class__.__name__


def summarize_error_response(status_code: int, body_text: str) -> str:
    body_text = body_text.strip()
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return f"{status_code} {body_text[:120]}"
    err = payload.get("error") or {}
    err_type = str(err.get("type") or "error").strip()
    message = str(err.get("message") or "").strip()
    request_id = str(payload.get("request_id") or "").strip()
    parts = [str(status_code), err_type]
    if message:
        parts.append(message[:88])
    if request_id:
        parts.append(f"req={request_id[:12]}")
    return " | ".join(parts)


def slugify(label: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in label).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or uuid.uuid4().hex[:8]


def security_find_password(service: str, account: str) -> Optional[str]:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def security_store_password(service: str, account: str, password: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", service, "-w", password],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to update keychain")


def discover_claude_client_id() -> Optional[str]:
    """Extract the OAuth client_id from the installed Claude Code binary."""
    import re
    import shutil

    env_val = os.environ.get("CLAUDE_OAUTH_CLIENT_ID", "").strip()
    if env_val:
        return env_val

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None
    real = Path(claude_bin).resolve()
    if not real.exists():
        return None
    try:
        raw = real.read_bytes()
        uuid_pat = rb'platform\.claude\.com/oauth/code/callback",CLIENT_ID:"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
        match = re.search(uuid_pat, raw)
        if match:
            return match.group(1).decode("ascii")
    except OSError:
        pass
    return None


_cached_claude_client_id: Optional[str] = None


def get_claude_client_id() -> str:
    global _cached_claude_client_id
    if _cached_claude_client_id is None:
        _cached_claude_client_id = discover_claude_client_id()
    if not _cached_claude_client_id:
        raise RuntimeError(
            "Could not discover Claude Code OAuth client_id. "
            "Set CLAUDE_OAUTH_CLIENT_ID in your environment, or set "
            "client_id in the account record."
        )
    return _cached_claude_client_id


def discover_codex_oauth() -> Optional[AccountRecord]:
    if not CODEX_AUTH_FILE.exists():
        return None
    try:
        data = json.loads(CODEX_AUTH_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tokens = data.get("tokens") or {}
    if not str(tokens.get("access_token") or "").strip():
        return None
    if not str(tokens.get("account_id") or "").strip():
        return None
    return AccountRecord(
        id="codex-oauth-local",
        name="Codex Local OAuth",
        provider="codex",
        auth_kind="oauth",
        visible=True,
        default_model="account-usage",
        models=["account-usage"],
        auth_file=str(CODEX_AUTH_FILE),
        sessions_dir=str(CODEX_SESSIONS_DIR),
        token_url=CODEX_TOKEN_URL,
        client_id=CODEX_CLIENT_ID,
        source="discovered",
        user_added=False,
    )


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
        token_url=CLAUDE_TOKEN_URL,
        client_id=discover_claude_client_id() or "",
        source="discovered",
        user_added=False,
    )


def merge_accounts(saved: list[AccountRecord], discovered: list[AccountRecord]) -> list[AccountRecord]:
    merged: dict[str, AccountRecord] = {account.id: account for account in saved}
    for account in discovered:
        existing = merged.get(account.id)
        if existing:
            existing.source = account.source
            existing.user_added = existing.user_added and account.user_added
            if account.auth_file:
                existing.auth_file = account.auth_file
            if account.sessions_dir:
                existing.sessions_dir = account.sessions_dir
            if account.keychain_service:
                existing.keychain_service = account.keychain_service
            if account.keychain_account:
                existing.keychain_account = account.keychain_account
            if not existing.user_added:
                existing.models = list(account.models)
                existing.default_model = account.default_model
            elif not existing.models:
                existing.models = account.models
            if not existing.default_model:
                existing.default_model = account.default_model
        else:
            merged[account.id] = account
    return list(merged.values())


def normalize_builtin_accounts(accounts: list[AccountRecord]) -> None:
    for account in accounts:
        if account.id == "claude-oauth-keychain" and not account.user_added:
            account.provider = "claude"
            account.auth_kind = "oauth"
            account.models = list(DEFAULT_CLAUDE_MODELS)
            account.default_model = DEFAULT_CLAUDE_MODELS[0]
            account.keychain_service = account.keychain_service or CLAUDE_KEYCHAIN_SERVICE
            account.keychain_account = account.keychain_account or getpass.getuser()
            account.token_url = account.token_url or CLAUDE_TOKEN_URL
            account.client_id = account.client_id or discover_claude_client_id() or ""
        if account.id == "codex-oauth-local" and not account.user_added:
            account.provider = "codex"
            account.auth_kind = "oauth"
            account.models = ["account-usage"]
            account.default_model = "account-usage"
            account.auth_file = account.auth_file or str(CODEX_AUTH_FILE)
            account.sessions_dir = account.sessions_dir or str(CODEX_SESSIONS_DIR)
            account.token_url = account.token_url or CODEX_TOKEN_URL
            account.client_id = account.client_id or CODEX_CLIENT_ID


def codex_auth_tokens(auth_file: Optional[str]) -> Optional[dict[str, Any]]:
    if not auth_file:
        return None
    path = Path(auth_file).expanduser()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    tokens = data.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return None
    return {"path": path, "raw": data, "tokens": tokens, "access_token": access_token, "account_id": account_id}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + os.linesep)


def refresh_codex_auth(session: requests.Session, record: AccountRecord) -> dict[str, Any]:
    auth = codex_auth_tokens(record.auth_file)
    if not auth:
        raise RuntimeError("missing Codex auth.json or required tokens")
    refresh_token = str(auth["tokens"].get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("auth.json has no refresh_token")
    response = session.post(
        record.token_url or CODEX_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": record.client_id or CODEX_CLIENT_ID,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("refresh response did not include access_token")
    auth["tokens"]["access_token"] = access_token
    if str(payload.get("refresh_token") or "").strip():
        auth["tokens"]["refresh_token"] = str(payload["refresh_token"]).strip()
    if str(payload.get("id_token") or "").strip():
        auth["tokens"]["id_token"] = str(payload["id_token"]).strip()
    auth["raw"]["tokens"] = auth["tokens"]
    auth["raw"]["last_refresh"] = iso_now()
    write_json(auth["path"], auth["raw"])
    return auth


def fetch_codex_usage(session: requests.Session, access_token: str, account_id: str) -> dict[str, Any]:
    response = session.get(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "Accept": "application/json",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def extract_nested(payload: dict[str, Any], path: str) -> dict[str, Any]:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return {}
        current = current.get(part)
    return current if isinstance(current, dict) else {}


def scan_codex_sessions(path_text: Optional[str]) -> dict[str, Any]:
    if not path_text:
        return {}
    directory = Path(path_text).expanduser()
    if not directory.exists():
        return {"error": f"missing: {directory}"}
    total = 0
    recent_5h = 0
    recent_7d = 0
    latest = None
    current = now_ts()
    for file_path in directory.rglob("*.jsonl"):
        total += 1
        modified = file_path.stat().st_mtime
        latest = max(latest or modified, modified)
        age = current - modified
        if age <= 5 * 3600:
            recent_5h += 1
        if age <= 7 * 24 * 3600:
            recent_7d += 1
    return {"total": total, "recent_5h": recent_5h, "recent_7d": recent_7d, "latest": latest}


def claude_keychain_payload(record: AccountRecord) -> Optional[dict[str, Any]]:
    service = record.keychain_service or CLAUDE_KEYCHAIN_SERVICE
    account = record.keychain_account or getpass.getuser()
    raw = security_find_password(service, account)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def refresh_claude_oauth(session: requests.Session, record: AccountRecord, payload: dict[str, Any]) -> dict[str, Any]:
    oauth = payload.get("claudeAiOauth") or {}
    refresh_token = str(oauth.get("refreshToken") or "").strip()
    if not refresh_token:
        raise RuntimeError("Claude keychain item has no refresh token")
    response = session.post(
        record.token_url or CLAUDE_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": record.client_id or get_claude_client_id(),
            "scope": CLAUDE_SCOPES,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    token_payload = response.json()
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("refresh response did not include access_token")
    oauth["accessToken"] = access_token
    if str(token_payload.get("refresh_token") or "").strip():
        oauth["refreshToken"] = str(token_payload["refresh_token"]).strip()
    expires_in = int(token_payload.get("expires_in") or 3600)
    oauth["expiresAt"] = int(now_ts() * 1000 + expires_in * 1000)
    payload["claudeAiOauth"] = oauth
    security_store_password(
        record.keychain_service or CLAUDE_KEYCHAIN_SERVICE,
        record.keychain_account or getpass.getuser(),
        json.dumps(payload),
    )
    return payload


def parse_claude_limits(headers: dict[str, str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for key, value in headers.items():
        if key.startswith("unified-") or key.startswith("anthropic-ratelimit-"):
            parsed[key] = value
            canonical = key
            if canonical.startswith("anthropic-ratelimit-"):
                canonical = canonical[len("anthropic-ratelimit-") :]
            parsed[canonical] = value
    return parsed


def parse_openai_limits(headers: dict[str, str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for key, value in headers.items():
        if key.startswith("x-ratelimit-"):
            parsed[key] = value
    return parsed


def fetch_claude_models(session: requests.Session, record: AccountRecord) -> list[str]:
    headers = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": CLAUDE_BETA_HEADER,
        "anthropic-dangerous-direct-browser-access": "true",
    }
    if record.auth_kind == "api":
        headers["Authorization"] = f"Bearer {record.api_key or ''}"
    else:
        payload = claude_keychain_payload(record)
        oauth = (payload or {}).get("claudeAiOauth") or {}
        token = str(oauth.get("accessToken") or "").strip()
        if not token:
            return []
        headers["Authorization"] = f"Bearer {token}"
    response = session.get(f"{CLAUDE_API_BASE}/v1/models", headers=headers, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        return []
    data = response.json().get("data") or []
    return sorted({str(item.get("id") or "").strip() for item in data if str(item.get("id") or "").strip()})


def fetch_openai_models(session: requests.Session, record: AccountRecord) -> list[str]:
    response = session.get(
        f"{record.api_base or OPENAI_API_BASE}/v1/models",
        headers={"Authorization": f"Bearer {record.api_key or ''}", "Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        return []
    data = response.json().get("data") or []
    return sorted({str(item.get("id") or "").strip() for item in data if str(item.get("id") or "").strip()})


def probe_openai_model(session: requests.Session, record: AccountRecord, model: str) -> ModelProbe:
    probe = ModelProbe(model=model, checked_at=now_ts())
    response = session.post(
        f"{record.api_base or OPENAI_API_BASE}/v1/responses",
        headers={
            "Authorization": f"Bearer {record.api_key or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": model,
            "input": "Reply with exactly: ok",
            "max_output_tokens": 1,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    headers = normalize_headers(response.headers)
    if response.status_code >= 400:
        probe.error = summarize_error_response(response.status_code, response.text)
        probe.status = "error"
        probe.rate_limits = parse_openai_limits(headers)
        return probe
    payload = response.json()
    probe.ok = True
    probe.status = "ok"
    probe.response_usage = payload.get("usage") or {}
    probe.rate_limits = parse_openai_limits(headers)
    probe.extra = {"id": payload.get("id"), "created_at": payload.get("created_at")}
    return probe


def probe_claude_model(
    session: requests.Session,
    record: AccountRecord,
    model: str,
    force_refresh: bool = False,
) -> ModelProbe:
    probe = ModelProbe(model=model, checked_at=now_ts())
    headers = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": CLAUDE_BETA_HEADER,
        "anthropic-dangerous-direct-browser-access": "true",
        "content-type": "application/json",
    }
    request_body: dict[str, Any] = {
        "model": model,
        "max_tokens": 64,
        "system": [{"type": "text", "text": CLAUDE_CODE_SYSTEM_TEXT}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            }
        ],
    }
    payload = None
    subscription_type = None
    rate_limit_tier = None
    if record.auth_kind == "api":
        headers["Authorization"] = f"Bearer {record.api_key or ''}"
    else:
        payload = claude_keychain_payload(record)
        oauth = (payload or {}).get("claudeAiOauth") or {}
        subscription_type = str(oauth.get("subscriptionType") or "").strip() or None
        rate_limit_tier = str(oauth.get("rateLimitTier") or "").strip() or None
        access_token = str(oauth.get("accessToken") or "").strip()
        expires_at = int(oauth.get("expiresAt") or 0)
        if force_refresh or (expires_at and int(now_ts() * 1000) >= expires_at):
            payload = refresh_claude_oauth(session, record, payload or {})
            oauth = payload.get("claudeAiOauth") or {}
            subscription_type = str(oauth.get("subscriptionType") or "").strip() or None
            rate_limit_tier = str(oauth.get("rateLimitTier") or "").strip() or None
            access_token = str(oauth.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("missing Claude OAuth access token")
        headers["Authorization"] = f"Bearer {access_token}"

    response = session.post(
        f"{CLAUDE_API_BASE}/v1/messages?beta=true",
        headers=headers,
        json=request_body,
        timeout=DEFAULT_TIMEOUT,
    )
    header_map = normalize_headers(response.headers)
    if response.status_code in {401, 403} and record.auth_kind == "oauth" and not force_refresh:
        return probe_claude_model(session, record, model, force_refresh=True)
    if response.status_code >= 400:
        probe.error = summarize_error_response(response.status_code, response.text)
        probe.status = "error"
        probe.rate_limits = parse_claude_limits(header_map)
        return probe
    body = response.json()
    probe.ok = True
    probe.status = header_map.get("unified-status", "ok")
    probe.response_usage = body.get("usage") or {}
    probe.rate_limits = parse_claude_limits(header_map)
    probe.extra = {
        "stop_reason": body.get("stop_reason"),
        "role": body.get("role"),
        "organization_id": header_map.get("anthropic-organization-id"),
        "subscription_type": subscription_type,
        "rate_limit_tier": rate_limit_tier,
    }
    return probe


def refresh_codex_oauth_account(session: requests.Session, state: AccountState) -> None:
    auth = codex_auth_tokens(state.record.auth_file)
    if not auth:
        raise RuntimeError("missing Codex auth.json")
    try:
        payload = fetch_codex_usage(session, auth["access_token"], auth["account_id"])
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code not in {401, 403}:
            raise
        auth = refresh_codex_auth(session, state.record)
        payload = fetch_codex_usage(session, auth["tokens"]["access_token"], auth["tokens"]["account_id"])

    primary = extract_nested(payload, "rate_limit.primary_window")
    secondary = extract_nested(payload, "rate_limit.secondary_window")
    review = extract_nested(payload, "code_review_rate_limit.primary_window")
    sessions = scan_codex_sessions(state.record.sessions_dir)
    state.summary = {
        "plan_type": payload.get("plan_type"),
        "primary_percent": primary.get("used_percent"),
        "primary_reset": primary.get("reset_after_seconds"),
        "secondary_percent": secondary.get("used_percent"),
        "secondary_reset": secondary.get("reset_after_seconds"),
        "review_percent": review.get("used_percent"),
        "credits": payload.get("credits") or {},
        "spend_control": payload.get("spend_control") or {},
        "sessions": sessions,
    }
    state.models = {
        "account-usage": ModelProbe(
            model="account-usage",
            ok=True,
            status="ok",
            checked_at=now_ts(),
            extra={"payload": payload},
        )
    }


def refresh_api_account(session: requests.Session, state: AccountState) -> None:
    if state.record.provider == "claude":
        if not state.available_models:
            state.available_models = fetch_claude_models(session, state.record) or list(DEFAULT_CLAUDE_MODELS)
        probes = {model: probe_claude_model(session, state.record, model) for model in state.record.active_models()}
    else:
        if not state.available_models:
            state.available_models = fetch_openai_models(session, state.record) or list(DEFAULT_OPENAI_MODELS)
        probes = {model: probe_openai_model(session, state.record, model) for model in state.record.active_models()}
    state.models = probes
    active = best_summary_probe(state)
    if active:
        state.summary = build_api_summary(state.record, active)


def refresh_claude_oauth_account(session: requests.Session, state: AccountState) -> None:
    if not state.available_models:
        state.available_models = fetch_claude_models(session, state.record) or list(DEFAULT_CLAUDE_MODELS)
    probes = {model: probe_claude_model(session, state.record, model) for model in state.record.active_models()}
    state.models = probes
    active = best_summary_probe(state)
    if active:
        state.summary = build_api_summary(state.record, active)


def best_summary_probe(state: AccountState) -> Optional[ModelProbe]:
    active = state.active_probe()
    probes = list(state.models.values())
    ranked = (
        [probe for probe in probes if probe.rate_limits]
        + [probe for probe in probes if probe.ok and not probe.rate_limits]
        + ([active] if active else [])
    )
    seen: set[str] = set()
    for probe in ranked:
        if not probe:
            continue
        key = probe.model
        if key in seen:
            continue
        seen.add(key)
        return probe
    return active


def build_api_summary(record: AccountRecord, probe: ModelProbe) -> dict[str, Any]:
    def percent_text(raw: Optional[str]) -> str:
        try:
            return f"{float(raw) * 100:.0f}%"
        except (TypeError, ValueError):
            return "-"

    summary = {
        "status": probe.status,
        "model": probe.model,
        "usage": probe.response_usage,
        "limits": probe.rate_limits,
        "error": probe.error,
    }
    if record.provider == "claude":
        util_5h = probe.rate_limits.get("unified-5h-utilization")
        util_7d = probe.rate_limits.get("unified-7d-utilization")
        summary["window_5h"] = percent_text(util_5h)
        summary["window_7d"] = percent_text(util_7d)
        summary["status_5h"] = probe.rate_limits.get("unified-5h-status", "-")
        summary["status_7d"] = probe.rate_limits.get("unified-7d-status", "-")
        summary["reset_5h"] = probe.rate_limits.get("unified-5h-reset", "-")
        summary["reset_7d"] = probe.rate_limits.get("unified-7d-reset", "-")
        summary["overage_status"] = probe.rate_limits.get("unified-overage-status", "-")
        summary["surpassed_5h"] = probe.rate_limits.get("unified-5h-surpassed-threshold", "-")
        summary["surpassed_7d"] = probe.rate_limits.get("unified-7d-surpassed-threshold", "-")
        summary["organization_id"] = probe.extra.get("organization_id")
        summary["subscription_type"] = probe.extra.get("subscription_type")
        summary["rate_limit_tier"] = probe.extra.get("rate_limit_tier")
    else:
        summary["requests_remaining"] = probe.rate_limits.get("x-ratelimit-remaining-requests", "-")
        summary["tokens_remaining"] = probe.rate_limits.get("x-ratelimit-remaining-tokens", "-")
    return summary


def refresh_state(session: requests.Session, state: AccountState, global_interval: float) -> None:
    state.last_refresh_started_at = now_ts()
    state.error = None
    try:
        if not state.record.enabled:
            state.error = "disabled"
        elif state.record.provider == "codex" and state.record.auth_kind == "oauth":
            refresh_codex_oauth_account(session, state)
        elif state.record.provider == "claude" and state.record.auth_kind == "oauth":
            refresh_claude_oauth_account(session, state)
        else:
            refresh_api_account(session, state)
        if not state.error:
            state.last_success_at = now_ts()
    except Exception as exc:
        state.error = compact_error(exc)
    state.last_refresh_finished_at = now_ts()
    state.next_refresh_at = state.last_refresh_finished_at + state.record.effective_refresh_interval(global_interval)


def account_snapshot(state: AccountState) -> str:
    if state.error:
        return state.error
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        primary = state.summary.get("primary_percent")
        secondary = state.summary.get("secondary_percent")
        plan = state.summary.get("plan_type") or "-"
        return f"plan {plan} | 5h {primary or 0}% | wk {secondary or 0}%"
    probe = state.active_probe()
    summary_probe_name = state.summary.get("model")
    if summary_probe_name:
        probe = state.models.get(summary_probe_name, probe)
    if not probe:
        return "waiting"
    if state.record.provider == "claude":
        return (
            f"{state.summary.get('model', probe.model)} | 5h {state.summary.get('window_5h', '-')}"
            f" ({state.summary.get('status_5h', '-')}) | 7d {state.summary.get('window_7d', '-')}"
        )
    if probe.error:
        return probe.error
    req = state.summary.get("requests_remaining", "-")
    tok = state.summary.get("tokens_remaining", "-")
    return f"{probe.model} | req rem {req} | tok rem {tok}"


def account_window_summary(state: AccountState, window: str) -> tuple[Optional[float], str, str]:
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        if window == "5h":
            percent = coerce_percent(state.summary.get("primary_percent"))
            reset = format_future_from_remaining(state.last_refresh_finished_at, state.summary.get("primary_reset"))
            return percent, format_percent_text(percent), reset
        percent = coerce_percent(state.summary.get("secondary_percent"))
        reset = format_future_from_remaining(state.last_refresh_finished_at, state.summary.get("secondary_reset"))
        return percent, format_percent_text(percent), reset

    key = "window_5h" if window == "5h" else "window_7d"
    reset_key = "reset_5h" if window == "5h" else "reset_7d"
    percent = coerce_percent(state.summary.get(key))
    reset = format_epoch_like(state.summary.get(reset_key))
    return percent, format_percent_text(percent), reset


def account_status_text(state: AccountState) -> str:
    if state.error:
        return state.error
    if state.record.provider == "claude":
        return f"{state.summary.get('status_5h', '-')}/{state.summary.get('status_7d', '-')}"
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        return str(state.summary.get("plan_type") or "ok")
    probe = state.models.get(state.summary.get("model") or state.active_model_name())
    if probe and probe.error:
        return probe.error
    return str(state.summary.get("status") or "ok")


def build_states(accounts: list[AccountRecord]) -> list[AccountState]:
    return [AccountState(record=account) for account in accounts]
