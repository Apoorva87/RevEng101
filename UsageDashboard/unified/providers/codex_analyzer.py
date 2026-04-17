"""Codex session analyzer — reads ~/.codex/sessions JSON files."""

from __future__ import annotations

import base64
import json
import shlex
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / ".local" / "usage_hub.json"
DEFAULT_PRESET = "7d"
UTC = timezone.utc
SHARED_ACCOUNT_KEY = "__shared__"


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def isoformat(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def bucket_start(value: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def compact_number(value: int) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def project_name_for(cwd: Optional[str]) -> str:
    if not cwd:
        return "(unknown)"
    path = Path(cwd)
    return path.name or str(path)


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def resume_command_for(session_id: str, cwd: Optional[str], cli_name: str = "codex") -> str:
    if cwd:
        return f"cd {shell_quote(cwd)} && {cli_name} resume {shell_quote(session_id)}"
    return f"{cli_name} resume {shell_quote(session_id)}"


def open_command_for(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return f"open {shell_quote(path)}"


def normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "reasoning_output_tokens": int(raw.get("reasoning_output_tokens") or 0),
        "total_tokens": int(raw.get("total_tokens") or 0),
    }


def diff_usage(current: dict[str, int], previous: Optional[dict[str, int]]) -> dict[str, int]:
    if not previous:
        return dict(current)
    diff: dict[str, int] = {}
    for key, value in current.items():
        diff[key] = max(0, value - int(previous.get(key) or 0))
    return diff


def empty_totals() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }


def add_totals(target: dict[str, int], delta: dict[str, int]) -> None:
    for key, value in delta.items():
        target[key] = int(target.get(key) or 0) + int(value or 0)


def decode_jwt_payload(token: Optional[str]) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def nested_get(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(Path(value).expanduser().resolve())


def account_label(name: str, email: Optional[str], account_id: Optional[str]) -> str:
    if email:
        return f"{name} [{email}]"
    if account_id:
        return f"{name} [{account_id[:8]}]"
    return name


@dataclass
class TokenDelta:
    timestamp: datetime
    bucket_day: datetime
    bucket_hour: datetime
    usage: dict[str, int]


@dataclass(frozen=True)
class CodexAccount:
    key: str
    name: str
    label: str
    account_id: Optional[str]
    email: Optional[str]
    auth_file: Optional[str]
    sessions_dir: Optional[str]
    source: str


class CodexSessionApp:
    def __init__(self, sessions_dir: Path, config_path: Path):
        self.sessions_dir = sessions_dir.expanduser()
        self.config_path = config_path.expanduser()
        self.lock = threading.RLock()

    def build_snapshot(
        self,
        preset: str = DEFAULT_PRESET,
        bucket: str = "day",
        start: Optional[str] = None,
        end: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            dataset = self._scan_sessions()
            window = self._resolve_window(preset, start, end)
            return self._build_payload(dataset, window, bucket, account)

    def _resolve_window(
        self,
        preset: str,
        start_raw: Optional[str],
        end_raw: Optional[str],
    ) -> tuple[datetime, datetime, str]:
        now = now_utc()
        preset = (preset or DEFAULT_PRESET).strip().lower()
        if preset == "custom":
            start = parse_timestamp(start_raw)
            end = parse_timestamp(end_raw)
            if not start or not end:
                raise ValueError("custom preset requires valid start and end timestamps")
            if end <= start:
                raise ValueError("end must be after start")
            return start, end, "custom"

        mapping = {
            "5h": timedelta(hours=5),
            "24h": timedelta(hours=24),
            "5d": timedelta(days=5),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "all": None,
        }
        if preset not in mapping:
            raise ValueError(f"unknown preset: {preset}")
        delta = mapping[preset]
        if delta is None:
            start = datetime(2020, 1, 1, tzinfo=UTC)
        else:
            start = now - delta
        return start, now, preset

    def _scan_sessions(self) -> dict[str, Any]:
        sessions: list[dict[str, Any]] = []
        errors: list[str] = []

        if not self.sessions_dir.exists():
            return {
                "sessions": sessions,
                "errors": errors,
                "sessions_dir_exists": False,
                "scanned_at": isoformat(now_utc()),
            }

        accounts = self._discover_accounts()

        for session_file in sorted(self.sessions_dir.glob("*.json")):
            try:
                parsed = self._parse_session_file(session_file, accounts)
                if parsed:
                    sessions.append(parsed)
            except Exception as exc:
                errors.append(f"{session_file.name}: {exc}")

        sessions.sort(key=lambda s: s.get("last_access") or "", reverse=True)

        return {
            "sessions": sessions,
            "errors": errors,
            "sessions_dir_exists": True,
            "scanned_at": isoformat(now_utc()),
        }

    def _discover_accounts(self) -> list[CodexAccount]:
        accounts: list[CodexAccount] = []

        # Try config file first
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
                for acct in config.get("accounts", []):
                    if acct.get("provider") not in ("codex", "openai"):
                        continue
                    key = acct.get("account_key") or acct.get("id") or ""
                    name = acct.get("name") or acct.get("label") or key
                    email = acct.get("email")
                    account_id = acct.get("account_id")
                    auth_file = acct.get("auth_file")
                    sessions_dir = acct.get("sessions_dir")
                    accounts.append(CodexAccount(
                        key=key,
                        name=name,
                        label=account_label(name, email, account_id),
                        account_id=account_id,
                        email=email,
                        auth_file=auth_file,
                        sessions_dir=sessions_dir,
                        source="config",
                    ))
            except Exception:
                pass

        # Fall back to ~/.codex/auth.json
        if not accounts:
            auth_path = Path.home() / ".codex" / "auth.json"
            if auth_path.exists():
                payload = self._read_auth_payload(auth_path)
                if payload:
                    email = payload.get("email") or payload.get("user", {}).get("email")
                    account_id = payload.get("account_id") or payload.get("user", {}).get("id")
                    name = email or account_id or "default"
                    accounts.append(CodexAccount(
                        key=SHARED_ACCOUNT_KEY,
                        name=name,
                        label=account_label(name, email, account_id),
                        account_id=account_id,
                        email=email,
                        auth_file=str(auth_path),
                        sessions_dir=None,
                        source="auth_file",
                    ))

        return accounts

    def _read_auth_payload(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            # Try to decode JWT access token
            access_token = raw.get("access_token") or raw.get("accessToken")
            if access_token:
                decoded = decode_jwt_payload(access_token)
                if decoded:
                    return {
                        "email": decoded.get("email") or nested_get(decoded, "https://api.openai.com/profile", "email"),
                        "account_id": decoded.get("sub") or decoded.get("account_id"),
                    }
            return raw
        except Exception:
            return {}

    def _match_account(self, file_path: Path, accounts: list[CodexAccount]) -> tuple[str, str]:
        for acct in accounts:
            sd = acct.sessions_dir
            if sd and str(file_path).startswith(str(Path(sd).expanduser())):
                return acct.key, acct.label
        if accounts:
            return accounts[0].key, accounts[0].label
        return SHARED_ACCOUNT_KEY, "Unknown"

    def _parse_session_file(self, file_path: Path, accounts: list[CodexAccount]) -> dict[str, Any]:
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

        if not isinstance(raw, dict):
            return {}

        session_id = raw.get("id") or file_path.stem
        cwd = raw.get("cwd") or raw.get("workingDirectory")
        project = raw.get("project") or project_name_for(cwd)
        label = raw.get("label") or raw.get("title") or session_id[:12]

        first_access_raw = raw.get("createdAt") or raw.get("created_at")
        last_access_raw = raw.get("updatedAt") or raw.get("updated_at") or raw.get("lastAccess")
        first_access = parse_timestamp(first_access_raw)
        last_access = parse_timestamp(last_access_raw)

        messages = raw.get("messages") or raw.get("history") or []
        user_messages = sum(1 for m in messages if m.get("role") == "user")
        agent_messages = sum(1 for m in messages if m.get("role") == "assistant")

        last_user_message: Optional[str] = None
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    last_user_message = content[:300]
                elif isinstance(content, list):
                    parts = [i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"]
                    last_user_message = " ".join(parts)[:300]
                break

        lifetime_usage = normalize_usage(raw.get("usage") or raw.get("lifetime_usage") or {})
        token_deltas: list[TokenDelta] = []
        for entry in raw.get("usage_history") or []:
            ts = parse_timestamp(entry.get("timestamp"))
            if not ts:
                continue
            token_deltas.append(TokenDelta(
                timestamp=ts,
                bucket_day=bucket_start(ts, "day"),
                bucket_hour=bucket_start(ts, "hour"),
                usage=normalize_usage(entry.get("usage") or {}),
            ))

        account_key, acct_label = self._match_account(file_path, accounts)

        cli_name = raw.get("cli") or "codex"
        duration_seconds = 0
        if first_access and last_access:
            duration_seconds = int((last_access - first_access).total_seconds())

        return {
            "session_id": session_id,
            "label": label,
            "project": project,
            "cwd": cwd,
            "working_path": normalize_path(cwd),
            "account_key": account_key,
            "account_label": acct_label,
            "first_access": isoformat(first_access),
            "last_access": isoformat(last_access),
            "user_messages": user_messages,
            "agent_messages": agent_messages,
            "last_user_message": last_user_message,
            "lifetime_usage": lifetime_usage,
            "token_deltas": [
                {
                    "timestamp": isoformat(d.timestamp),
                    "bucket_day": isoformat(d.bucket_day),
                    "bucket_hour": isoformat(d.bucket_hour),
                    "usage": d.usage,
                }
                for d in token_deltas
            ],
            "duration_seconds": duration_seconds,
            "originator": raw.get("originator"),
            "cli_version": raw.get("version") or raw.get("cli_version"),
            "model_provider": raw.get("model_provider") or raw.get("provider"),
            "context_window": raw.get("context_window"),
            "resume_command": resume_command_for(session_id, cwd, cli_name),
            "open_command": open_command_for(cwd),
            "file_path": str(file_path),
        }

    def usage_snapshot(self) -> dict[str, Any]:
        with self.lock:
            dataset = self._scan_sessions()
        sessions = dataset.get("sessions", [])
        usage_events: list[dict[str, Any]] = []
        limit_events: list[dict[str, Any]] = []

        for s in sessions:
            for delta in s.get("token_deltas") or []:
                ts_str = delta.get("timestamp")
                ts = parse_timestamp(ts_str)
                if not ts:
                    continue
                usage_events.append({
                    "ts": ts.timestamp(),
                    "session_id": s["session_id"],
                    "project_path": s.get("cwd"),
                    "account_key": s.get("account_key"),
                    "model": s.get("model_provider"),
                    "provider": "codex",
                    **delta.get("usage", {}),
                    "total_tokens": delta.get("usage", {}).get("total_tokens", 0),
                })

        return {
            "provider": "codex",
            "scanned_at": dataset.get("scanned_at"),
            "usage_events": usage_events,
            "limit_events": limit_events,
        }

    def _build_payload(
        self,
        dataset: dict[str, Any],
        window: tuple[datetime, datetime, str],
        bucket: str,
        account_filter: Optional[str],
    ) -> dict[str, Any]:
        start, end, preset = window
        all_sessions: list[dict[str, Any]] = dataset.get("sessions", [])

        if account_filter and account_filter != "all":
            all_sessions = [s for s in all_sessions if s.get("account_key") == account_filter]

        selected_sessions = [
            s for s in all_sessions
            if (
                parse_timestamp(s.get("last_access")) or datetime.min.replace(tzinfo=UTC)
            ) >= start
        ]

        summary = empty_totals()
        for s in selected_sessions:
            add_totals(summary, s.get("lifetime_usage") or {})

        projects: dict[str, dict[str, Any]] = {}
        for s in selected_sessions:
            proj = s.get("project") or "(unknown)"
            if proj not in projects:
                projects[proj] = {"project": proj, "sessions": 0, "totals": empty_totals()}
            projects[proj]["sessions"] += 1
            add_totals(projects[proj]["totals"], s.get("lifetime_usage") or {})

        projects_payload = sorted(
            projects.values(),
            key=lambda p: p["totals"].get("total_tokens", 0),
            reverse=True,
        )

        timeline: dict[str, dict[str, int]] = defaultdict(empty_totals)
        for s in selected_sessions:
            for delta in s.get("token_deltas") or []:
                ts = parse_timestamp(delta.get("timestamp"))
                if not ts or ts < start or ts > end:
                    continue
                key = isoformat(bucket_start(ts, bucket)) or ""
                add_totals(timeline[key], delta.get("usage") or {})

        timeline_payload = [
            {"bucket": k, **v}
            for k, v in sorted(timeline.items())
        ]

        recent_sessions = sorted(
            selected_sessions,
            key=lambda s: s.get("last_access") or "",
            reverse=True,
        )

        return {
            "scanned_at": dataset.get("scanned_at"),
            "sessions_dir_exists": dataset.get("sessions_dir_exists", False),
            "selected_account": account_filter or "all",
            "window": {
                "preset": preset,
                "bucket": bucket,
                "start": isoformat(start),
                "end": isoformat(end),
                "duration_seconds": int((end - start).total_seconds()),
            },
            "summary": {
                "totals": summary,
                "session_count": len(selected_sessions),
                "project_count": len(projects_payload),
                "latest_access": recent_sessions[0]["last_access"] if recent_sessions else None,
                "max_context_window": max((row["context_window"] or 0) for row in selected_sessions) if selected_sessions else 0,
            },
            "recent_sessions": recent_sessions[:10],
            "projects": projects_payload,
            "sessions": selected_sessions,
            "timeline": timeline_payload,
        }
