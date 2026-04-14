#!/usr/bin/env python3
"""Unified terminal dashboard for Claude and Codex/OpenAI accounts."""

from __future__ import annotations

import argparse
import curses
import getpass
import json
import locale
import os
import subprocess
import time
import uuid
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+",
)

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

KEY_HELP = (
    "q:quit  r:refresh all  enter:refresh sel  a:add  x:hide/show  d:delete  "
    "m:models  +/-:global rf  ]/[:acct rf  j/k:nav  left/right:fold  v:hidden  g:grid  h:help"
)

USAGE_BAR_WIDTH = 10
ITEM_CELL_WIDTH = 28
USAGE_CELL_WIDTH = 16
RESET_CELL_WIDTH = 14
REVIEW_CELL_WIDTH = 6
EXTRA_CELL_WIDTH = 30
STATUS_CELL_WIDTH = 30


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Claude and Codex/OpenAI usage dashboard.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"Config file path. Default: {CONFIG_PATH}")
    parser.add_argument("--no-startup-prompt", action="store_true", help="Skip the pre-dashboard account prompt.")
    return parser.parse_args()


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + ">"


def safe_addstr(stdscr: "curses._CursesWindow", y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    available = max(0, width - x)
    if available <= 0:
        return
    try:
        stdscr.addstr(y, x, clip(text, available), attr)
    except curses.error:
        return


def format_timestamp(epoch_seconds: Optional[float]) -> str:
    if not epoch_seconds:
        return "-"
    return datetime.fromtimestamp(epoch_seconds).astimezone().strftime("%Y-%m-%d %H:%M:%S")


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


def draw_bar(percent: Optional[float], width: int) -> str:
    if width <= 0:
        return ""
    if percent is None:
        return "-" * width
    clamped = max(0.0, min(999.0, float(percent)))
    filled = min(width, int(round((min(clamped, 100.0) / 100.0) * width)))
    return "#" * filled + "-" * max(0, width - filled)


def get_color_for_percent(percent: Optional[float]) -> int:
    if percent is None:
        return curses.color_pair(0)
    if percent >= 100:
        return curses.color_pair(3)
    if percent >= 75:
        return curses.color_pair(2)
    return curses.color_pair(1)


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


def format_reset_cell(reset_text: str) -> str:
    return clip(reset_text or "-", RESET_CELL_WIDTH)


def normalize_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> dict[str, str]:
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
    """Extract the OAuth client_id from the installed Claude Code binary.

    Runs `claude /dev/null 2>&1` to capture the auth error which leaks the
    client_id, or greps the binary for UUIDs near oauth context.
    Falls back to checking environment variable CLAUDE_OAUTH_CLIENT_ID.
    """
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
        # In the compiled Claude Code binary the production OAuth config
        # appears as:  ...platform.claude.com/oauth/code/callback",CLIENT_ID:"<uuid>"
        # The dev config uses a template URL instead, so we anchor on the
        # production domain to pick the right one.
        uuid_pat = rb'platform\.claude\.com/oauth/code/callback",CLIENT_ID:"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
        match = re.search(uuid_pat, raw)
        if match:
            return match.group(1).decode("ascii")
    except OSError:
        pass
    return None


_cached_claude_client_id: Optional[str] = None


def get_claude_client_id() -> str:
    """Return the Claude Code OAuth client_id, discovering it if needed."""
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


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def prompt_text(prompt: str, default: Optional[str] = None) -> str:
    if default:
        raw = input(f"{prompt} [{default}] ").strip()
        return raw or default
    return input(f"{prompt} ").strip()


def prompt_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt} ")


def prompt_models(provider: str, existing: Optional[list[str]] = None) -> list[str]:
    seed = ",".join(existing or (DEFAULT_CLAUDE_MODELS if provider == "claude" else DEFAULT_OPENAI_MODELS[:2]))
    raw = prompt_text("Comma-separated models to probe", seed)
    models = [item.strip() for item in raw.split(",") if item.strip()]
    return models or (existing or [])


def account_display_name(record: AccountRecord) -> str:
    return f"{record.name} ({record.provider_label()}/{record.auth_kind})"


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


def visible_states(states: list[AccountState], show_hidden: bool) -> list[AccountState]:
    if show_hidden:
        return states
    return [state for state in states if state.record.visible]


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


def account_review_text(state: AccountState) -> str:
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        return format_percent_text(state.summary.get("review_percent"))
    return "-"


def account_extra_text(state: AccountState) -> str:
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        credits = state.summary.get("credits") or {}
        spend_control = state.summary.get("spend_control") or {}
        balance = credits.get("balance", "-")
        spend = "HIT" if spend_control.get("reached") else "ok"
        return f"credits={balance}  spend_ctrl={spend}"
    if state.record.provider == "claude":
        return f"overage={state.summary.get('overage_status', '-')}"
    return (
        f"req_rem={state.summary.get('requests_remaining', '-')}  "
        f"tok_rem={state.summary.get('tokens_remaining', '-')}"
    )


def account_status_text(state: AccountState) -> str:
    if state.error:
        return state.error
    if state.record.provider == "claude":
        return (
            f"{state.summary.get('status_5h', '-')}/"
            f"{state.summary.get('status_7d', '-')}"
        )
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        return str(state.summary.get("plan_type") or "ok")
    probe = state.models.get(state.summary.get("model") or state.active_model_name())
    if probe and probe.error:
        return probe.error
    return str(state.summary.get("status") or "ok")


def model_window_summary(probe: ModelProbe, window: str) -> tuple[Optional[float], str, str]:
    util_key = "unified-5h-utilization" if window == "5h" else "unified-7d-utilization"
    reset_key = "unified-5h-reset" if window == "5h" else "unified-7d-reset"
    raw_util = probe.rate_limits.get(util_key)
    percent = None
    if raw_util is not None:
        try:
            percent = float(raw_util) * 100.0
        except ValueError:
            percent = None
    return percent, format_percent_text(percent), format_epoch_like(probe.rate_limits.get(reset_key))


def draw_help_popup(stdscr: "curses._CursesWindow") -> None:
    lines = [
        "Unified Usage Hub",
        "",
        "This dashboard auto-discovers local OAuth accounts for Codex and Claude when they exist.",
        "API-key accounts can be added at startup or later with the add-account flow.",
        "",
        "Keys",
        "q: quit",
        "r: refresh every account now",
        "Enter: refresh only the selected account",
        "a: add a new Claude or Codex/OpenAI API-key account",
        "m: edit the selected account's probe model list",
        "x: hide or show the selected account in the main display",
        "d: delete a saved account that you added",
        "+ / -: change global refresh interval",
        "] / [: change selected account refresh interval",
        "Left: fold selected account",
        "Right: expand selected account",
        "v: toggle whether hidden accounts stay visible in the list",
        "g: toggle grid view",
        "",
        "Press any key to return.",
    ]
    stdscr.erase()
    for idx, line in enumerate(lines):
        safe_addstr(stdscr, idx + 1, 2, line, curses.A_BOLD if idx == 0 else 0)
    stdscr.refresh()
    stdscr.getch()


def draw_dashboard(
    stdscr: "curses._CursesWindow",
    states: list[AccountState],
    selected_index: int,
    config: ConfigStore,
    status_message: str,
    show_hidden: bool,
    expanded_ids: set[str],
    grid_view: bool,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    shown = visible_states(states, show_hidden)
    ok_count = sum(1 for state in states if state.last_success_at and not state.error)
    err_count = sum(1 for state in states if state.error and state.error != "disabled")
    safe_addstr(stdscr, 0, 0, "usage-hub", curses.A_BOLD | curses.color_pair(4))
    safe_addstr(
        stdscr,
        1,
        0,
        clip(
            f"accounts={len(states)} shown={len(shown)} ok={ok_count} errors={err_count} global_refresh={config.global_refresh_interval:.0f}s hidden={'on' if show_hidden else 'off'} view={'grid' if grid_view else 'tree'}",
            width,
        ),
        curses.A_DIM,
    )
    safe_addstr(stdscr, 2, 0, clip(KEY_HELP, width), curses.A_DIM)

    if grid_view:
        draw_grid_dashboard(stdscr, shown, selected_index, status_message)
        return

    # Column separator for visual clarity
    SEP = " | "

    safe_addstr(
        stdscr,
        4,
        0,
        clip(
            f"  {'Account':<{ITEM_CELL_WIDTH}}"
            f"{SEP}{'5h Usage':<{USAGE_CELL_WIDTH}}"
            f" {'Reset 5h':<{RESET_CELL_WIDTH}}"
            f"{SEP}{'7d Usage':<{USAGE_CELL_WIDTH}}"
            f" {'Reset 7d':<{RESET_CELL_WIDTH}}"
            f"{SEP}{'Rev':>{REVIEW_CELL_WIDTH}}"
            f"{SEP}{'Extra':<{EXTRA_CELL_WIDTH}}"
            f"{SEP}{'Status':<{STATUS_CELL_WIDTH}}",
            width,
        ),
        curses.A_BOLD | curses.A_UNDERLINE,
    )

    row_y = 5
    current_y = row_y
    for idx, state in enumerate(shown):
        if current_y >= height - 3:
            break
        selected = idx == selected_index
        attr = curses.A_REVERSE if selected else curses.A_NORMAL
        if state.error:
            attr |= curses.color_pair(3)
        elif state.last_success_at:
            attr |= curses.color_pair(1)

        p5, p5_text, r5 = account_window_summary(state, "5h")
        p7, p7_text, r7 = account_window_summary(state, "7d")
        tree_mark = "v " if state.record.id in expanded_ids else "> "
        kind = f"{state.record.provider_label()}/{state.record.auth_kind}"
        item_label = clip(f"{tree_mark}{state.record.name} ({kind})", ITEM_CELL_WIDTH)

        usage_5 = f"{draw_bar(p5, USAGE_BAR_WIDTH)} {p5_text:>4}"
        usage_7 = f"{draw_bar(p7, USAGE_BAR_WIDTH)} {p7_text:>4}"
        review = clip(account_review_text(state), REVIEW_CELL_WIDTH)
        extra = clip(account_extra_text(state), EXTRA_CELL_WIDTH)
        status = clip(account_status_text(state), STATUS_CELL_WIDTH)
        main_line = (
            f"  {item_label:<{ITEM_CELL_WIDTH}}"
            f"{SEP}{usage_5:<{USAGE_CELL_WIDTH}}"
            f" {format_reset_cell(r5):<{RESET_CELL_WIDTH}}"
            f"{SEP}{usage_7:<{USAGE_CELL_WIDTH}}"
            f" {format_reset_cell(r7):<{RESET_CELL_WIDTH}}"
            f"{SEP}{review:>{REVIEW_CELL_WIDTH}}"
            f"{SEP}{extra:<{EXTRA_CELL_WIDTH}}"
            f"{SEP}{status:<{STATUS_CELL_WIDTH}}"
        )
        safe_addstr(stdscr, current_y, 0, clip(main_line, width), attr)
        current_y += 1

        if state.record.id not in expanded_ids:
            continue

        # Info sub-row: refresh timing
        rf_interval = state.record.effective_refresh_interval(config.global_refresh_interval)
        next_in = format_countdown(max(0, state.next_refresh_at - now_ts()))
        last_ok = format_relative(state.last_success_at)
        info_detail = f"refresh={rf_interval:.0f}s  next={next_in}  last_ok={last_ok}  src={state.record.source}  models={len(state.record.active_models())}"
        if current_y < height - 2:
            info_line = f"    {'  info':<{ITEM_CELL_WIDTH}}{SEP}{info_detail}"
            safe_addstr(stdscr, current_y, 0, clip(info_line, width), curses.A_DIM)
            current_y += 1

        if state.record.provider == "claude":
            org = state.summary.get('organization_id') or '-'
            sub = state.summary.get('subscription_type') or '-'
            tier = state.summary.get('rate_limit_tier') or '-'
            overage = state.summary.get('overage_status') or '-'
            identity = f"org={org}  sub={sub}  tier={tier}  overage={overage}"
            if current_y < height - 2:
                auth_line = f"    {'  auth':<{ITEM_CELL_WIDTH}}{SEP}{identity}"
                safe_addstr(stdscr, current_y, 0, clip(auth_line, width), curses.A_DIM)
                current_y += 1

        if state.record.provider == "codex" and state.record.auth_kind == "oauth":
            sessions = state.summary.get("sessions") or {}
            credits = state.summary.get("credits") or {}
            spend_control = state.summary.get("spend_control") or {}
            detail = (
                f"credits={credits.get('balance', '-')}  has={credits.get('has_credits', '-')}  "
                f"unlimited={credits.get('unlimited', '-')}  spend={spend_control.get('reached', '-')}  "
                f"sessions(5h/7d/all)={sessions.get('recent_5h', '-')}/{sessions.get('recent_7d', '-')}/{sessions.get('total', '-')}"
            )
            if current_y < height - 2:
                detail_line = f"    {'  details':<{ITEM_CELL_WIDTH}}{SEP}{detail}"
                safe_addstr(stdscr, current_y, 0, clip(detail_line, width), curses.A_DIM)
                current_y += 1
            continue

        for model_index, model in enumerate(state.record.active_models()):
            if current_y >= height - 2:
                break
            probe = state.models.get(model)
            is_last = model_index == len(state.record.active_models()) - 1
            branch = "`-" if is_last else "|-"
            model_label = clip(f"  {branch} {model}", ITEM_CELL_WIDTH)
            if not probe:
                empty_line = (
                    f"    {model_label:<{ITEM_CELL_WIDTH}}"
                    f"{SEP}{'':<{USAGE_CELL_WIDTH}}"
                    f" {'':<{RESET_CELL_WIDTH}}"
                    f"{SEP}{'':<{USAGE_CELL_WIDTH}}"
                    f" {'':<{RESET_CELL_WIDTH}}"
                    f"{SEP}{'':{REVIEW_CELL_WIDTH}}"
                    f"{SEP}{'':<{EXTRA_CELL_WIDTH}}"
                    f"{SEP}{'waiting':<{STATUS_CELL_WIDTH}}"
                )
                safe_addstr(stdscr, current_y, 0, clip(empty_line, width), curses.A_DIM)
                current_y += 1
                continue
            mp5, mp5_text, mr5 = model_window_summary(probe, "5h")
            mp7, mp7_text, mr7 = model_window_summary(probe, "7d")
            m_status = probe.status if not probe.error else probe.error
            model_usage_5 = f"{draw_bar(mp5, USAGE_BAR_WIDTH)} {mp5_text:>4}"
            model_usage_7 = f"{draw_bar(mp7, USAGE_BAR_WIDTH)} {mp7_text:>4}"
            model_line = (
                f"    {model_label:<{ITEM_CELL_WIDTH}}"
                f"{SEP}{model_usage_5:<{USAGE_CELL_WIDTH}}"
                f" {format_reset_cell(mr5):<{RESET_CELL_WIDTH}}"
                f"{SEP}{model_usage_7:<{USAGE_CELL_WIDTH}}"
                f" {format_reset_cell(mr7):<{RESET_CELL_WIDTH}}"
                f"{SEP}{'':{REVIEW_CELL_WIDTH}}"
                f"{SEP}{'':<{EXTRA_CELL_WIDTH}}"
                f"{SEP}{clip(m_status, STATUS_CELL_WIDTH):<{STATUS_CELL_WIDTH}}"
            )
            safe_addstr(stdscr, current_y, 0, clip(model_line, width), curses.A_DIM)
            current_y += 1

    safe_addstr(stdscr, height - 1, 0, clip(status_message, width), curses.A_DIM)
    stdscr.refresh()


def draw_grid_dashboard(
    stdscr: "curses._CursesWindow",
    shown: list[AccountState],
    selected_index: int,
    status_message: str,
) -> None:
    height, width = stdscr.getmaxyx()
    card_w = 46 if width >= 96 else max(30, width - 2)
    cols = max(1, width // (card_w + 2))
    safe_addstr(stdscr, 4, 0, clip("Grid View", width), curses.A_BOLD | curses.A_UNDERLINE)
    start_y = 5
    card_h = 7
    for idx, state in enumerate(shown):
        row = idx // cols
        col = idx % cols
        y = start_y + row * card_h
        x = col * (card_w + 2)
        if y >= height - 2:
            break
        p5, p5_text, r5 = account_window_summary(state, "5h")
        p7, p7_text, r7 = account_window_summary(state, "7d")
        attr = curses.A_REVERSE if idx == selected_index else curses.A_NORMAL
        if state.error:
            attr |= curses.color_pair(3)
        elif state.last_success_at:
            attr |= curses.color_pair(1)
        title = f"{state.record.name} ({state.record.provider_label()}/{state.record.auth_kind})"
        safe_addstr(stdscr, y, x, clip(title, card_w), attr | curses.A_BOLD)
        safe_addstr(stdscr, y + 1, x, "-" * min(card_w, len(title)), curses.A_DIM)
        safe_addstr(stdscr, y + 2, x, clip(f"  5h: {draw_bar(p5, 10)} {p5_text:>4}  resets {r5}", card_w), attr)
        safe_addstr(stdscr, y + 3, x, clip(f"  7d: {draw_bar(p7, 10)} {p7_text:>4}  resets {r7}", card_w), attr)
        review_text = account_review_text(state)
        extra_text = account_extra_text(state)
        if review_text != "-":
            safe_addstr(stdscr, y + 4, x, clip(f"  Review: {review_text}  {extra_text}", card_w), attr)
        else:
            safe_addstr(stdscr, y + 4, x, clip(f"  {extra_text}", card_w), attr)
        safe_addstr(stdscr, y + 5, x, clip(f"  Status: {account_status_text(state)}", card_w), attr)
    safe_addstr(stdscr, height - 1, 0, clip(status_message, width), curses.A_DIM)
    stdscr.refresh()


def build_states(accounts: list[AccountRecord]) -> list[AccountState]:
    return [AccountState(record=account) for account in accounts]


def format_epoch_like(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def account_prompt_flow(store: ConfigStore) -> bool:
    changed = False
    print()
    print("Discovered local OAuth accounts will be loaded automatically when available.")
    print()
    print("==============================\n")
    if prompt_yes_no("Add a Claude API-key account now?"):
        account = interactive_account_create("claude")
        if account:
            store.accounts.append(account)
            changed = True
    if prompt_yes_no("Add a Codex/OpenAI API-key account now?"):
        account = interactive_account_create("codex")
        if account:
            store.accounts.append(account)
            changed = True
    return changed


def interactive_account_create(provider: str) -> Optional[AccountRecord]:
    provider_label = "Claude" if provider == "claude" else "Codex/OpenAI"
    print()
    print(f"Adding {provider_label} API-key account")
    name = prompt_text("Account label", f"{provider_label} API")
    api_key = prompt_secret("API key")
    if not api_key.strip():
        print("No key entered. Skipping.")
        return None
    models = prompt_models(provider)
    default_model = models[0] if models else None
    return AccountRecord(
        id=f"{provider}-api-{slugify(name)}-{uuid.uuid4().hex[:6]}",
        name=name,
        provider=provider,
        auth_kind="api",
        visible=True,
        default_model=default_model,
        models=models,
        api_key=api_key.strip(),
        api_base=CLAUDE_API_BASE if provider == "claude" else OPENAI_API_BASE,
        source="saved",
        user_added=True,
    )


def with_prompt_pause(stdscr: "curses._CursesWindow") -> None:
    curses.def_prog_mode()
    curses.endwin()


def restore_after_prompt(stdscr: "curses._CursesWindow") -> None:
    curses.reset_prog_mode()
    stdscr.clear()
    stdscr.refresh()


def add_account_from_ui(stdscr: "curses._CursesWindow", store: ConfigStore) -> tuple[Optional[AccountRecord], str]:
    with_prompt_pause(stdscr)
    try:
        provider_raw = prompt_text("Provider to add (claude/codex)", "claude").strip().lower()
        provider = "codex" if provider_raw.startswith("c") and provider_raw != "claude" else "claude"
        account = interactive_account_create(provider)
        if not account:
            return None, "Add-account flow cancelled."
        store.accounts.append(account)
        store.save()
        return account, f"Added {account_display_name(account)}"
    finally:
        restore_after_prompt(stdscr)


def edit_models_from_ui(
    stdscr: "curses._CursesWindow",
    store: ConfigStore,
    state: AccountState,
) -> str:
    if state.record.provider == "codex" and state.record.auth_kind == "oauth":
        return "Codex OAuth uses account-level usage, so there is no model list to edit."
    with_prompt_pause(stdscr)
    try:
        suggestions = ", ".join(state.available_models[:12]) if state.available_models else "(no discovered model list yet)"
        print()
        print(f"Known models: {suggestions}")
        models = prompt_models(state.record.provider, state.record.active_models())
        state.record.models = models
        state.record.default_model = models[0] if models else None
        for index, record in enumerate(store.accounts):
            if record.id == state.record.id:
                store.accounts[index] = state.record
                break
        store.save()
        return f"Updated models for {state.record.name}"
    finally:
        restore_after_prompt(stdscr)


def delete_account(store: ConfigStore, state: AccountState) -> str:
    if not state.record.user_added:
        return "Only user-added API-key accounts can be deleted."
    store.accounts = [account for account in store.accounts if account.id != state.record.id]
    store.save()
    return f"Deleted {state.record.name}"


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)


def main_loop(stdscr: "curses._CursesWindow", store: ConfigStore, states: list[AccountState]) -> None:
    init_colors()
    stdscr.nodelay(True)
    stdscr.keypad(True)
    selected_index = 0
    show_hidden = False
    expanded_ids = {state.record.id for state in states}
    grid_view = False
    status_message = "Refreshing accounts..."
    session = requests.Session()

    for state in states:
        refresh_state(session, state, store.global_refresh_interval)
    status_message = "Loaded current account snapshots."

    while True:
        shown = visible_states(states, show_hidden)
        if shown:
            selected_index = max(0, min(selected_index, len(shown) - 1))
        else:
            selected_index = 0

        current_time = now_ts()
        for state in states:
            if state.next_refresh_at <= current_time:
                refresh_state(session, state, store.global_refresh_interval)

        draw_dashboard(stdscr, states, selected_index, store, status_message, show_hidden, expanded_ids, grid_view)
        key = stdscr.getch()
        if key == -1:
            time.sleep(0.1)
            continue

        shown = visible_states(states, show_hidden)
        current_state = shown[selected_index] if shown else None

        if key in {ord("q"), ord("Q"), 3, 27}:
            break
        if key in {ord("h"), ord("?")}:
            draw_help_popup(stdscr)
            status_message = "Returned from help."
        elif key in {ord("r")}:
            for state in states:
                refresh_state(session, state, store.global_refresh_interval)
            status_message = "Refreshed all accounts."
        elif key in {10, 13, curses.KEY_ENTER} and current_state:
            refresh_state(session, current_state, store.global_refresh_interval)
            status_message = f"Refreshed {current_state.record.name}"
        elif key in {curses.KEY_UP, ord("k")} and shown:
            selected_index = (selected_index - 1) % len(shown)
        elif key in {curses.KEY_DOWN, ord("j"), 9} and shown:
            selected_index = (selected_index + 1) % len(shown)
        elif key == curses.KEY_LEFT and current_state:
            expanded_ids.discard(current_state.record.id)
            status_message = f"Folded {current_state.record.name}"
        elif key == curses.KEY_RIGHT and current_state:
            expanded_ids.add(current_state.record.id)
            status_message = f"Expanded {current_state.record.name}"
        elif key == ord("+"):
            store.global_refresh_interval = min(600.0, store.global_refresh_interval + 5.0)
            store.save()
            status_message = f"Global refresh set to {store.global_refresh_interval:.0f}s"
        elif key == ord("-"):
            store.global_refresh_interval = max(5.0, store.global_refresh_interval - 5.0)
            store.save()
            status_message = f"Global refresh set to {store.global_refresh_interval:.0f}s"
        elif key == ord("]") and current_state:
            current_state.record.refresh_interval = min(
                600.0,
                (current_state.record.refresh_interval or store.global_refresh_interval) + 5.0,
            )
            store.save()
            status_message = f"{current_state.record.name} refresh set to {current_state.record.refresh_interval:.0f}s"
        elif key == ord("[") and current_state:
            current_state.record.refresh_interval = max(
                5.0,
                (current_state.record.refresh_interval or store.global_refresh_interval) - 5.0,
            )
            store.save()
            status_message = f"{current_state.record.name} refresh set to {current_state.record.refresh_interval:.0f}s"
        elif key == ord("x") and current_state:
            current_state.record.visible = not current_state.record.visible
            if not current_state.record.visible:
                show_hidden = True
            store.save()
            status_message = f"{current_state.record.name} visible={current_state.record.visible}"
        elif key == ord("v"):
            show_hidden = not show_hidden
            status_message = f"Show hidden accounts is now {'on' if show_hidden else 'off'}."
        elif key == ord("g"):
            grid_view = not grid_view
            status_message = f"View is now {'grid' if grid_view else 'tree'}."
        elif key == ord("a"):
            new_account, status_message = add_account_from_ui(stdscr, store)
            if new_account:
                states.append(AccountState(record=new_account))
                expanded_ids.add(new_account.id)
                refresh_state(session, states[-1], store.global_refresh_interval)
        elif key == ord("m") and current_state:
            status_message = edit_models_from_ui(stdscr, store, current_state)
            refresh_state(session, current_state, store.global_refresh_interval)
        elif key == ord("d") and current_state:
            deleted_id = current_state.record.id
            status_message = delete_account(store, current_state)
            states[:] = [state for state in states if state.record.id != deleted_id]
            selected_index = 0
        else:
            status_message = f"Unknown key: {key}"


def load_store_and_accounts(args: argparse.Namespace) -> tuple[ConfigStore, list[AccountRecord]]:
    store = ConfigStore(args.config.expanduser())
    store.load()
    discovered = [item for item in [discover_codex_oauth(), discover_claude_oauth()] if item]
    accounts = merge_accounts(store.accounts, discovered)
    normalize_builtin_accounts(accounts)
    if accounts and not any(account.visible for account in accounts):
        for account in accounts:
            account.visible = True
    store.accounts = accounts
    return store, accounts


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args()
    store, accounts = load_store_and_accounts(args)
    if not args.no_startup_prompt and os.isatty(0):
        changed = account_prompt_flow(store)
        if changed:
            accounts = store.accounts
    store.save()

    if not os.isatty(0) or not os.isatty(1):
        print("usage_hub.py needs an interactive terminal because it uses curses.")
        return 1

    states = build_states(accounts)
    curses.wrapper(main_loop, store, states)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
