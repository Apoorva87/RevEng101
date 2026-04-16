#!/usr/bin/env python3
"""Local web dashboard for Codex session history and token usage."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import threading
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8877
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / ".local" / "usage_hub.json"
DEFAULT_PRESET = "7d"
UTC = timezone.utc
SHARED_ACCOUNT_KEY = "__shared__"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex session dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host. Default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port. Default: {DEFAULT_PORT}")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help=f"Codex sessions directory. Default: {DEFAULT_SESSIONS_DIR}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Usage hub config for account discovery. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the dashboard in a browser.")
    return parser.parse_args()


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
        if preset == "all":
            start = now - timedelta(days=3650)
            return start, now, "all"
        delta = mapping[preset]
        assert delta is not None
        return now - delta, now, preset

    def _scan_sessions(self) -> dict[str, Any]:
        sessions_dir = self.sessions_dir
        accounts = self._discover_accounts()
        if not sessions_dir.exists():
            return {
                "sessions_dir": str(sessions_dir),
                "exists": False,
                "scanned_at": isoformat(now_utc()),
                "accounts": accounts,
                "sessions": [],
                "token_events": [],
                "limit_events": [],
            }

        sessions: list[dict[str, Any]] = []
        token_events: list[dict[str, Any]] = []
        limit_events: list[dict[str, Any]] = []

        for file_path in sorted(sessions_dir.rglob("*.jsonl")):
            session = self._parse_session_file(file_path, accounts)
            sessions.append(session)
            for delta in session["token_deltas"]:
                token_events.append(
                    {
                        "session_id": session["session_id"],
                        "session_label": session["session_label"],
                        "project": session["project"],
                        "cwd": session["cwd"],
                        "account_key": session["account_key"],
                        "account_label": session["account_label"],
                        "timestamp": delta.timestamp,
                        "bucket_day": delta.bucket_day,
                        "bucket_hour": delta.bucket_hour,
                        "usage": delta.usage,
                    }
                )
            for limit_event in session.get("rate_limit_snapshots", []):
                limit_events.append(
                    {
                        "session_id": session["session_id"],
                        "session_label": session["session_label"],
                        "project": session["project"],
                        "cwd": session["cwd"],
                        "account_key": session["account_key"],
                        "account_label": session["account_label"],
                        **limit_event,
                    }
                )

        return {
            "sessions_dir": str(sessions_dir),
            "exists": True,
            "scanned_at": isoformat(now_utc()),
            "accounts": accounts,
            "sessions": sessions,
            "token_events": token_events,
            "limit_events": limit_events,
        }

    def _discover_accounts(self) -> list[CodexAccount]:
        discovered: dict[str, CodexAccount] = {}
        raw_accounts: list[dict[str, Any]] = []
        if self.config_path.exists():
            try:
                payload = json.loads(self.config_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
            config_accounts = payload.get("accounts") or []
            if isinstance(config_accounts, list):
                raw_accounts = [item for item in config_accounts if isinstance(item, dict)]

        for raw in raw_accounts:
            if str(raw.get("provider") or "").strip().lower() != "codex":
                continue
            auth_file = normalize_path(raw.get("auth_file"))
            sessions_dir = normalize_path(raw.get("sessions_dir"))
            auth_payload = self._read_auth_payload(Path(auth_file)) if auth_file else {}
            account_id = str(
                auth_payload.get("account_id")
                or nested_get(auth_payload, "access_payload", "https://api.openai.com/auth", "chatgpt_account_id")
                or nested_get(auth_payload, "id_payload", "https://api.openai.com/auth", "chatgpt_account_id")
                or ""
            ).strip() or None
            email = str(
                nested_get(auth_payload, "access_payload", "https://api.openai.com/profile", "email")
                or nested_get(auth_payload, "id_payload", "email")
                or ""
            ).strip() or None
            key = str(raw.get("id") or "").strip() or account_id or auth_file or "codex-account"
            name = str(raw.get("name") or "Codex Account").strip()
            discovered[key] = CodexAccount(
                key=key,
                name=name,
                label=account_label(name, email, account_id),
                account_id=account_id,
                email=email,
                auth_file=auth_file,
                sessions_dir=sessions_dir,
                source=str(raw.get("source") or "config"),
            )

        default_auth = normalize_path(str(Path.home() / ".codex" / "auth.json"))
        if default_auth and default_auth not in {account.auth_file for account in discovered.values()}:
            auth_payload = self._read_auth_payload(Path(default_auth))
            account_id = str(auth_payload.get("account_id") or "").strip() or None
            email = str(
                nested_get(auth_payload, "access_payload", "https://api.openai.com/profile", "email")
                or nested_get(auth_payload, "id_payload", "email")
                or ""
            ).strip() or None
            key = account_id or "codex-oauth-local"
            name = "Codex Local OAuth"
            discovered[key] = CodexAccount(
                key=key,
                name=name,
                label=account_label(name, email, account_id),
                account_id=account_id,
                email=email,
                auth_file=default_auth,
                sessions_dir=normalize_path(str(self.sessions_dir)),
                source="discovered",
            )

        return sorted(discovered.values(), key=lambda item: item.label.lower())

    def _read_auth_payload(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        tokens = raw.get("tokens") or {}
        if not isinstance(tokens, dict):
            return {}
        return {
            "account_id": str(tokens.get("account_id") or "").strip(),
            "id_payload": decode_jwt_payload(tokens.get("id_token")),
            "access_payload": decode_jwt_payload(tokens.get("access_token")),
        }

    def _match_account(self, file_path: Path, accounts: list[CodexAccount]) -> tuple[str, str]:
        file_resolved = str(file_path.expanduser().resolve())
        matches: list[tuple[int, CodexAccount]] = []
        for account in accounts:
            if not account.sessions_dir:
                continue
            session_root = account.sessions_dir.rstrip("/")
            if file_resolved == session_root or file_resolved.startswith(session_root + "/"):
                matches.append((len(session_root), account))
        if not matches:
            return SHARED_ACCOUNT_KEY, "Shared / Unassigned"
        matches.sort(key=lambda item: item[0], reverse=True)
        best_prefix = matches[0][0]
        best_accounts = [account for length, account in matches if length == best_prefix]
        if len(best_accounts) == 1:
            return best_accounts[0].key, best_accounts[0].label
        return SHARED_ACCOUNT_KEY, "Shared / Unassigned"

    def _parse_session_file(self, file_path: Path, accounts: list[CodexAccount]) -> dict[str, Any]:
        session_meta: dict[str, Any] = {}
        token_deltas: list[TokenDelta] = []
        rate_limit_snapshots: list[dict[str, Any]] = []
        previous_total: Optional[dict[str, int]] = None
        first_access: Optional[datetime] = None
        last_access: Optional[datetime] = None
        user_messages = 0
        agent_messages = 0
        turn_contexts = 0
        last_user_message = ""
        last_agent_message = ""
        latest_context_window: Optional[int] = None

        with file_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = parse_timestamp(item.get("timestamp"))
                if timestamp:
                    first_access = min(first_access or timestamp, timestamp)
                    last_access = max(last_access or timestamp, timestamp)

                item_type = item.get("type")
                payload = item.get("payload") or {}

                if item_type == "session_meta" and isinstance(payload, dict):
                    meta_id = str(payload.get("id") or "").strip()
                    if not session_meta or meta_id == file_path.stem:
                        session_meta = payload
                    meta_timestamp = parse_timestamp(payload.get("timestamp"))
                    if meta_timestamp:
                        first_access = min(first_access or meta_timestamp, meta_timestamp)
                        last_access = max(last_access or meta_timestamp, meta_timestamp)
                    continue

                if item_type == "turn_context":
                    turn_contexts += 1
                    continue

                if item_type != "event_msg" or not isinstance(payload, dict):
                    continue

                subtype = payload.get("type")
                if subtype == "user_message":
                    user_messages += 1
                    last_user_message = str(payload.get("message") or "").strip()
                elif subtype == "agent_message":
                    agent_messages += 1
                    last_agent_message = str(payload.get("message") or "").strip()
                elif subtype == "token_count" and timestamp:
                    rate_limits = payload.get("rate_limits") or {}
                    if isinstance(rate_limits, dict):
                        primary = rate_limits.get("primary") or {}
                        secondary = rate_limits.get("secondary") or {}
                        rate_limit_snapshots.append(
                            {
                                "timestamp": timestamp,
                                "primary_used_percent": float(primary.get("used_percent")) if primary.get("used_percent") is not None else None,
                                "primary_window_minutes": int(primary.get("window_minutes") or 0) or None,
                                "primary_resets_at": int(primary.get("resets_at") or 0) or None,
                                "secondary_used_percent": float(secondary.get("used_percent")) if secondary.get("used_percent") is not None else None,
                                "secondary_window_minutes": int(secondary.get("window_minutes") or 0) or None,
                                "secondary_resets_at": int(secondary.get("resets_at") or 0) or None,
                                "plan_type": str(rate_limits.get("plan_type") or "").strip() or None,
                            }
                        )
                    info = payload.get("info") or {}
                    current_total = normalize_usage(info.get("total_token_usage") or {})
                    latest_context_window = int(info.get("model_context_window") or latest_context_window or 0) or latest_context_window
                    delta = diff_usage(current_total, previous_total)
                    token_deltas.append(
                        TokenDelta(
                            timestamp=timestamp,
                            bucket_day=bucket_start(timestamp, "day"),
                            bucket_hour=bucket_start(timestamp, "hour"),
                            usage=delta,
                        )
                    )
                    previous_total = current_total

        cwd = str(session_meta.get("cwd") or "")
        session_id = str(session_meta.get("id") or file_path.stem)
        started_at = parse_timestamp(session_meta.get("timestamp")) or first_access or last_access
        lifetime_usage = previous_total or empty_totals()
        account_key, account_name = self._match_account(file_path, accounts)
        working_path = cwd or str(file_path.parent)

        return {
            "session_id": session_id,
            "session_label": file_path.stem,
            "file_path": str(file_path),
            "working_path": working_path,
            "cwd": cwd,
            "project": project_name_for(cwd),
            "account_key": account_key,
            "account_label": account_name,
            "originator": session_meta.get("originator"),
            "cli_version": session_meta.get("cli_version"),
            "model_provider": session_meta.get("model_provider"),
            "started_at": started_at,
            "first_access": first_access or started_at,
            "last_access": last_access or started_at,
            "duration_seconds": (
                max(0.0, (last_access - (started_at or last_access)).total_seconds()) if last_access and started_at else 0.0
            ),
            "user_messages": user_messages,
            "agent_messages": agent_messages,
            "turn_contexts": turn_contexts,
            "last_user_message": last_user_message[:220],
            "last_agent_message": last_agent_message[:220],
            "context_window": latest_context_window,
            "lifetime_usage": lifetime_usage,
            "resume_command": resume_command_for(session_id, cwd or None),
            "open_command": open_command_for(working_path),
            "token_deltas": token_deltas,
            "rate_limit_snapshots": rate_limit_snapshots,
        }

    def usage_snapshot(self) -> dict[str, Any]:
        with self.lock:
            dataset = self._scan_sessions()

        usage_events = []
        for event in dataset.get("token_events", []):
            usage = dict(event.get("usage") or {})
            usage_events.append(
                {
                    "provider": "codex",
                    "timestamp": event["timestamp"].timestamp() if hasattr(event.get("timestamp"), "timestamp") else None,
                    "session_id": event.get("session_id"),
                    "session_label": event.get("session_label"),
                    "project_name": event.get("project") or "(unknown)",
                    "project_path": event.get("cwd") or event.get("project") or "(unknown)",
                    "account_key": event.get("account_key") or "unknown",
                    "account_label": event.get("account_label") or "Unknown",
                    "usage": {
                        **usage,
                        "cached_tokens": int(usage.get("cached_input_tokens") or 0),
                    },
                }
            )

        limit_events = []
        for event in dataset.get("limit_events", []):
            limit_events.append(
                {
                    "provider": "codex",
                    "timestamp": event["timestamp"].timestamp() if hasattr(event.get("timestamp"), "timestamp") else None,
                    "session_id": event.get("session_id"),
                    "session_label": event.get("session_label"),
                    "project_name": event.get("project") or "(unknown)",
                    "project_path": event.get("cwd") or event.get("project") or "(unknown)",
                    "account_key": event.get("account_key") or "unknown",
                    "account_label": event.get("account_label") or "Unknown",
                    "kind": "rate_limits",
                    "plan_type": event.get("plan_type"),
                    "primary_used_percent": event.get("primary_used_percent"),
                    "primary_window_minutes": event.get("primary_window_minutes"),
                    "primary_resets_at": event.get("primary_resets_at"),
                    "secondary_used_percent": event.get("secondary_used_percent"),
                    "secondary_window_minutes": event.get("secondary_window_minutes"),
                    "secondary_resets_at": event.get("secondary_resets_at"),
                }
            )

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
        account: Optional[str],
    ) -> dict[str, Any]:
        if bucket not in {"hour", "day"}:
            raise ValueError("bucket must be 'hour' or 'day'")

        start, end, preset = window
        sessions = dataset["sessions"]
        token_events = dataset["token_events"]
        account_options = [
            {
                "key": item.key,
                "label": item.label,
                "account_id": item.account_id,
                "email": item.email,
                "sessions_dir": item.sessions_dir,
                "source": item.source,
            }
            for item in dataset["accounts"]
        ]
        if any(session["account_key"] == SHARED_ACCOUNT_KEY for session in sessions):
            account_options.append(
                {
                    "key": SHARED_ACCOUNT_KEY,
                    "label": "Shared / Unassigned",
                    "account_id": None,
                    "email": None,
                    "sessions_dir": dataset["sessions_dir"],
                    "source": "derived",
                }
            )

        available_keys = ["all", *[item["key"] for item in account_options]]
        selected_account = account if account in available_keys else "all"

        filtered_events = [
            event
            for event in token_events
            if start <= event["timestamp"] <= end
            and (selected_account == "all" or event["account_key"] == selected_account)
        ]
        filtered_sessions = [
            session for session in sessions if selected_account == "all" or session["account_key"] == selected_account
        ]

        session_window_usage: dict[str, dict[str, int]] = defaultdict(empty_totals)
        bucket_usage: dict[datetime, dict[str, Any]] = {}

        for event in filtered_events:
            usage = event["usage"]
            add_totals(session_window_usage[event["session_id"]], usage)
            bucket_key = event["bucket_hour"] if bucket == "hour" else event["bucket_day"]
            row = bucket_usage.setdefault(
                bucket_key,
                {
                    "usage": empty_totals(),
                    "projects": defaultdict(empty_totals),
                    "sessions": defaultdict(empty_totals),
                    "session_labels": {},
                    "session_meta": {},
                },
            )
            add_totals(row["usage"], usage)
            add_totals(row["projects"][event["project"]], usage)
            add_totals(row["sessions"][event["session_id"]], usage)
            row["session_labels"][event["session_id"]] = event["session_label"]
            row["session_meta"][event["session_id"]] = {
                "project": event["project"],
                "cwd": event["cwd"],
                "account_key": event["account_key"],
                "account_label": event["account_label"],
            }

        selected_sessions: list[dict[str, Any]] = []
        selected_projects: dict[str, dict[str, Any]] = {}
        recent_sessions: list[dict[str, Any]] = []

        for session in filtered_sessions:
            last_access = session["last_access"]
            in_window = bool(session_window_usage.get(session["session_id"])) or (
                last_access and start <= last_access <= end
            )
            if not in_window:
                continue

            window_usage = dict(session_window_usage.get(session["session_id"]) or empty_totals())
            session_row = {
                "session_id": session["session_id"],
                "label": session["session_label"],
                "project": session["project"],
                "cwd": session["cwd"],
                "file_path": session["file_path"],
                "working_path": session["working_path"],
                "account_key": session["account_key"],
                "account_label": session["account_label"],
                "started_at": isoformat(session["started_at"]),
                "first_access": isoformat(session["first_access"]),
                "last_access": isoformat(last_access),
                "duration_seconds": int(session["duration_seconds"]),
                "user_messages": session["user_messages"],
                "agent_messages": session["agent_messages"],
                "turn_contexts": session["turn_contexts"],
                "context_window": session["context_window"],
                "originator": session["originator"],
                "cli_version": session["cli_version"],
                "model_provider": session["model_provider"],
                "last_user_message": session["last_user_message"],
                "last_agent_message": session["last_agent_message"],
                "resume_command": session["resume_command"],
                "open_command": session["open_command"],
                "window_usage": window_usage,
                "lifetime_usage": session["lifetime_usage"],
            }
            selected_sessions.append(session_row)
            recent_sessions.append(session_row)

            project = selected_projects.setdefault(
                session["project"],
                {
                    "project": session["project"],
                    "cwd_values": set(),
                    "session_count": 0,
                    "last_access": None,
                    "window_usage": empty_totals(),
                    "lifetime_usage": empty_totals(),
                    "top_session": "",
                },
            )
            project["cwd_values"].add(session["cwd"])
            project["session_count"] += 1
            if last_access and (project["last_access"] is None or last_access > project["last_access"]):
                project["last_access"] = last_access
            add_totals(project["window_usage"], window_usage)
            add_totals(project["lifetime_usage"], session["lifetime_usage"])

        for project in selected_projects.values():
            top_session = max(
                (row for row in selected_sessions if row["project"] == project["project"]),
                key=lambda row: row["window_usage"]["total_tokens"],
                default=None,
            )
            project["top_session"] = top_session["label"] if top_session else ""

        selected_sessions.sort(
            key=lambda row: (row["window_usage"]["total_tokens"], row["last_access"] or ""),
            reverse=True,
        )
        recent_sessions.sort(key=lambda row: row["last_access"] or "", reverse=True)

        projects_payload = [
            {
                "project": project["project"],
                "cwd": sorted(value for value in project["cwd_values"] if value),
                "session_count": project["session_count"],
                "last_access": isoformat(project["last_access"]),
                "window_usage": project["window_usage"],
                "lifetime_usage": project["lifetime_usage"],
                "top_session": project["top_session"],
            }
            for project in sorted(
                selected_projects.values(),
                key=lambda row: (row["window_usage"]["total_tokens"], row["last_access"] or datetime.min.replace(tzinfo=UTC)),
                reverse=True,
            )
        ]

        timeline_payload = []
        for bucket_key in sorted(bucket_usage.keys(), reverse=True):
            row = bucket_usage[bucket_key]
            top_projects = sorted(
                ({"name": name, "usage": usage} for name, usage in row["projects"].items()),
                key=lambda item: item["usage"]["total_tokens"],
                reverse=True,
            )[:4]
            top_sessions = sorted(
                ({"name": row["session_labels"].get(name, name), "usage": usage} for name, usage in row["sessions"].items()),
                key=lambda item: item["usage"]["total_tokens"],
                reverse=True,
            )[:4]
            timeline_payload.append(
                {
                    "bucket_start": isoformat(bucket_key),
                    "usage": row["usage"],
                    "project_count": len(row["projects"]),
                    "session_count": len(row["sessions"]),
                    "top_projects": top_projects,
                    "top_sessions": top_sessions,
                    "project_rows": sorted(
                        (
                            {"project": name, "usage": usage}
                            for name, usage in row["projects"].items()
                        ),
                        key=lambda item: item["usage"]["total_tokens"],
                        reverse=True,
                    ),
                    "session_rows": sorted(
                        (
                            {
                                "session_id": session_id,
                                "label": row["session_labels"].get(session_id, session_id),
                                "project": (row["session_meta"].get(session_id) or {}).get("project"),
                                "cwd": (row["session_meta"].get(session_id) or {}).get("cwd"),
                                "account_key": (row["session_meta"].get(session_id) or {}).get("account_key"),
                                "account_label": (row["session_meta"].get(session_id) or {}).get("account_label"),
                                "usage": usage,
                            }
                            for session_id, usage in row["sessions"].items()
                        ),
                        key=lambda item: item["usage"]["total_tokens"],
                        reverse=True,
                    ),
                }
            )

        summary = empty_totals()
        for event in filtered_events:
            add_totals(summary, event["usage"])

        account_summaries: dict[str, dict[str, Any]] = {}
        all_totals = empty_totals()
        all_window_events = [event for event in token_events if start <= event["timestamp"] <= end]
        for event in token_events:
            if start <= event["timestamp"] <= end:
                add_totals(all_totals, event["usage"])
        account_summaries["all"] = {
            "key": "all",
            "label": "All Accounts",
            "account_id": None,
            "email": None,
            "sessions_dir": dataset["sessions_dir"],
            "source": "derived",
            "window_usage": all_totals,
            "session_count": len(sessions),
            "window_session_count": len({event["session_id"] for event in all_window_events}),
        }
        for option in account_options:
            key = option["key"]
            account_events = [
                event for event in token_events if start <= event["timestamp"] <= end and event["account_key"] == key
            ]
            totals = empty_totals()
            for event in account_events:
                add_totals(totals, event["usage"])
            account_summaries[key] = {
                **option,
                "window_usage": totals,
                "session_count": len([session for session in sessions if session["account_key"] == key]),
                "window_session_count": len({event["session_id"] for event in account_events}),
            }

        return {
            "scanned_at": dataset["scanned_at"],
            "sessions_dir": dataset["sessions_dir"],
            "sessions_dir_exists": dataset["exists"],
            "accounts": [account_summaries[key] for key in available_keys],
            "selected_account": selected_account,
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


def html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Sessions Dashboard</title>
  <style>
    :root{
      --bg:#08111d;
      --bg2:#0d1726;
      --panel:#101d2f;
      --line:rgba(155,188,222,.16);
      --ink:#e9f3ff;
      --muted:#9bb1c8;
      --accent:#37c7a7;
      --accent2:#62a7ff;
      --danger:#ff7a7a;
      --shadow:0 18px 50px rgba(0,0,0,.28);
    }

    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color:var(--ink);
      background:
        radial-gradient(circle at top left, rgba(55,199,167,.16), transparent 28%),
        radial-gradient(circle at top right, rgba(98,167,255,.14), transparent 24%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg2) 55%, #07101b 100%);
    }

    .shell{max-width:1480px;margin:0 auto;padding:24px 20px 40px}
    .hero, .panel, .stat{
      background:linear-gradient(180deg, rgba(18,31,49,.95), rgba(12,24,40,.95));
      border:1px solid var(--line);
      border-radius:20px;
      box-shadow:var(--shadow);
    }
    .hero{padding:24px}
    .eyebrow{
      text-transform:uppercase;
      letter-spacing:.12em;
      font-size:12px;
      color:var(--accent);
      margin-bottom:10px;
      font-weight:700;
    }
    h1{margin:0;font-size:clamp(30px,4vw,48px);line-height:1.05}
    .sub{margin:12px 0 0;color:var(--muted);max-width:880px;font-size:15px;line-height:1.5}
    .controls{
      margin-top:18px;
      display:grid;
      grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
      gap:12px;
      align-items:end;
    }
    .control-span{grid-column:1 / -1}
    label{display:flex;flex-direction:column;gap:6px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
    select,input,button{
      border-radius:12px;
      border:1px solid rgba(155,188,222,.18);
      background:#0a1524;
      color:var(--ink);
      padding:12px 14px;
      font:inherit;
    }
    button{
      cursor:pointer;
      background:linear-gradient(135deg, rgba(55,199,167,.22), rgba(98,167,255,.2));
      font-weight:700;
    }
    button.secondary{background:#0a1524}
    .summary{
      margin-top:18px;
      display:grid;
      grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
      gap:12px;
    }
    .stat{padding:16px}
    .stat .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
    .stat .value{margin-top:8px;font-size:30px;font-weight:800}
    .stat .meta{margin-top:6px;color:var(--muted);font-size:13px}
    .main{
      margin-top:18px;
      display:grid;
      grid-template-columns:280px minmax(0,1fr);
      gap:16px;
    }
    .sidebar,.content{display:flex;flex-direction:column;gap:16px}
    .panel{padding:16px}
    .tabs{display:flex;gap:8px;flex-wrap:wrap}
    .tab.active{outline:2px solid rgba(98,167,255,.45)}
    .list{display:flex;flex-direction:column;gap:10px}
    .mini{
      padding:12px;
      border-radius:14px;
      border:1px solid rgba(155,188,222,.12);
      background:rgba(7,16,27,.55);
    }
    .mini .title{font-weight:700}
    .mini .meta{margin-top:6px;color:var(--muted);font-size:13px;line-height:1.45}
    .table-wrap{overflow:auto}
    table{width:100%;border-collapse:collapse;font-size:14px}
    th,td{padding:12px 10px;border-bottom:1px solid rgba(155,188,222,.1);text-align:left;vertical-align:top}
    th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;position:sticky;top:0;background:#102035}
    td .subline{display:block;color:var(--muted);font-size:12px;line-height:1.4;margin-top:4px}
    .mono{font-family:ui-monospace, SFMono-Regular, Menlo, monospace}
    .hidden{display:none}
    .pill{
      display:inline-flex;
      align-items:center;
      gap:6px;
      padding:5px 9px;
      border-radius:999px;
      background:rgba(98,167,255,.12);
      color:#cfe3ff;
      font-size:12px;
      font-weight:700;
    }
    .footer{margin-top:10px;color:var(--muted);font-size:12px}
    .error{color:var(--danger);font-weight:700}
    .chart-shell{
      margin:12px 0;
      border:1px solid rgba(155,188,222,.12);
      border-radius:18px;
      background:linear-gradient(180deg, rgba(8,17,29,.92), rgba(10,21,36,.92));
      padding:10px;
      overflow:hidden;
    }
    .chart-shell svg{display:block;width:100%;height:auto}
    .chart-meta,.chart-legend{
      display:flex;
      flex-wrap:wrap;
      gap:12px;
      color:var(--muted);
      font-size:12px;
    }
    .legend-chip{display:inline-flex;align-items:center;gap:6px}
    .legend-swatch{width:12px;height:12px;border-radius:999px;display:inline-block}
    @media (max-width: 1100px){
      .main{grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">Codex Local Sessions</div>
      <h1>Session timelines, token windows, and account-scoped rollups</h1>
      <p class="sub">
        This dashboard reads local Codex session logs, tries to map them to a single Codex account where possible,
        and keeps every page view scoped to one account tab at a time. Sessions that cannot be safely attributed are
        kept in a separate shared bucket instead of being mixed into a named account.
      </p>
      <div class="controls">
        <div class="control-span">
          <label>Account View</label>
          <div class="tabs" id="accountTabs"></div>
        </div>
        <label>Preset
          <select id="preset">
            <option value="5h">Last 5 hours</option>
            <option value="24h">Last 24 hours</option>
            <option value="5d">Last 5 days</option>
            <option value="7d" selected>Last 7 days</option>
            <option value="30d">Last 30 days</option>
            <option value="all">All observed</option>
            <option value="custom">Custom range</option>
          </select>
        </label>
        <label>Bucket
          <select id="bucket">
            <option value="day" selected>Day</option>
            <option value="hour">Hour</option>
          </select>
        </label>
        <label>Start
          <input id="start" type="datetime-local">
        </label>
        <label>End
          <input id="end" type="datetime-local">
        </label>
        <button id="refresh">Refresh View</button>
        <button id="now" class="secondary">Jump To Now</button>
      </div>
    </section>

    <section class="summary" id="summary"></section>

    <section class="main">
      <aside class="sidebar">
        <section class="panel">
          <div class="eyebrow">Views</div>
          <div class="tabs">
            <button class="tab active" data-view="projects">Projects</button>
            <button class="tab" data-view="sessions">Sessions</button>
            <button class="tab" data-view="timeline">Timeline</button>
          </div>
          <div class="footer" id="windowLabel"></div>
        </section>
        <section class="panel">
          <div class="eyebrow">Recent Access</div>
          <div class="list" id="recentSessions"></div>
        </section>
        <section class="panel">
          <div class="eyebrow">Source</div>
          <div class="footer" id="sourceMeta"></div>
        </section>
      </aside>

      <div class="content">
        <section class="panel view" data-view="projects">
          <div class="eyebrow">Project Breakdown</div>
          <div class="table-wrap"><table id="projectsTable"></table></div>
        </section>
        <section class="panel view hidden" data-view="sessions">
          <div class="eyebrow">Session Breakdown</div>
          <div class="table-wrap"><table id="sessionsTable"></table></div>
        </section>
        <section class="panel view hidden" data-view="timeline">
          <div class="eyebrow">Token Timeline</div>
          <div class="tabs" id="metricTabs">
            <button class="tab active" data-metric="total_tokens">Total Tokens</button>
            <button class="tab" data-metric="input_tokens">Input Tokens</button>
            <button class="tab" data-metric="output_tokens">Output Tokens</button>
          </div>
          <div class="chart-shell" id="timelineChart"></div>
          <div class="chart-meta" id="timelineMeta"></div>
          <div class="chart-legend">
            <span class="legend-chip"><span class="legend-swatch" style="background:#62a7ff"></span>Selected metric</span>
            <span class="legend-chip"><span class="legend-swatch" style="background:#37c7a7"></span>Bucket points</span>
          </div>
          <div class="table-wrap"><table id="timelineTable"></table></div>
        </section>
      </div>
    </section>
  </div>

  <script>
    const fmtInt = new Intl.NumberFormat();
    const fmtDateTime = new Intl.DateTimeFormat([], {dateStyle:'medium', timeStyle:'short'});
    const fmtDateOnly = new Intl.DateTimeFormat([], {dateStyle:'medium'});
    let currentAccount = null;
    let currentMetric = 'total_tokens';

    function compact(n){
      const abs = Math.abs(n);
      if (abs >= 1_000_000_000) return (n / 1_000_000_000).toFixed(1) + 'B';
      if (abs >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
      if (abs >= 1_000) return (n / 1_000).toFixed(1) + 'K';
      return String(n);
    }

    function formatStamp(value, kind='datetime'){
      if (!value) return '-';
      const date = new Date(value);
      return kind === 'date' ? fmtDateOnly.format(date) : fmtDateTime.format(date);
    }

    function escapeHtml(value){
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function tokenLine(usage){
      if (!usage) return '-';
      return `${compact(usage.total_tokens || 0)} total`;
    }

    function tokenDetail(usage){
      if (!usage) return '-';
      return `in ${compact(usage.input_tokens || 0)} | cached ${compact(usage.cached_input_tokens || 0)} | out ${compact(usage.output_tokens || 0)} | reason ${compact(usage.reasoning_output_tokens || 0)}`;
    }

    function readControls(){
      const preset = document.getElementById('preset').value;
      const bucket = document.getElementById('bucket').value;
      const startRaw = document.getElementById('start').value;
      const endRaw = document.getElementById('end').value;
      const params = new URLSearchParams({preset, bucket});
      if (currentAccount) params.set('account', currentAccount);
      if (preset === 'custom') {
        if (startRaw) params.set('start', new Date(startRaw).toISOString());
        if (endRaw) params.set('end', new Date(endRaw).toISOString());
      }
      return params;
    }

    function setNowRange(){
      const end = new Date();
      const start = new Date(end.getTime() - 7 * 24 * 3600 * 1000);
      const local = value => {
        const copy = new Date(value.getTime() - value.getTimezoneOffset() * 60000);
        return copy.toISOString().slice(0,16);
      };
      document.getElementById('preset').value = '7d';
      document.getElementById('bucket').value = 'day';
      document.getElementById('start').value = local(start);
      document.getElementById('end').value = local(end);
    }

    function renderAccountTabs(data){
      const rows = data.accounts || [];
      if (!currentAccount || !rows.some(row => row.key === currentAccount)) {
        currentAccount = data.selected_account;
      }
      document.getElementById('accountTabs').innerHTML = rows.map(row => `
        <button class="tab ${row.key === data.selected_account ? 'active' : ''}" data-account="${escapeHtml(row.key)}">
          ${escapeHtml(row.label)}
        </button>
      `).join('');
      document.querySelectorAll('#accountTabs [data-account]').forEach(button => {
        button.addEventListener('click', () => {
          currentAccount = button.dataset.account;
          loadSnapshot().catch(err => alert(err.message));
        });
      });
    }

    function renderSummary(data){
      const totals = data.summary.totals;
      const selectedAccount = (data.accounts || []).find(item => item.key === data.selected_account);
      const cards = [
        ['Selected Total', compact(totals.total_tokens || 0), tokenDetail(totals)],
        ['Input Tokens', compact(totals.input_tokens || 0), `Cached: ${compact(totals.cached_input_tokens || 0)}`],
        ['Output Tokens', compact(totals.output_tokens || 0), `Reasoning: ${compact(totals.reasoning_output_tokens || 0)}`],
        ['Projects', fmtInt.format(data.summary.project_count || 0), `Sessions: ${fmtInt.format(data.summary.session_count || 0)}`],
        ['Latest Access', formatStamp(data.summary.latest_access), `Context window max: ${compact(data.summary.max_context_window || 0)}`],
        ['Account', selectedAccount ? selectedAccount.label : '-', selectedAccount ? `${fmtInt.format(selectedAccount.window_session_count || 0)} active sessions in window` : '-'],
        ['Window Span', compact(data.window.duration_seconds || 0) + 's', `${formatStamp(data.window.start)} to ${formatStamp(data.window.end)}`],
      ];
      document.getElementById('summary').innerHTML = cards.map(([label, value, meta]) => `
        <div class="stat">
          <div class="label">${escapeHtml(label)}</div>
          <div class="value">${escapeHtml(value)}</div>
          <div class="meta">${escapeHtml(meta)}</div>
        </div>
      `).join('');
      document.getElementById('windowLabel').textContent = `Preset: ${data.window.preset} | Bucket: ${data.window.bucket} | ${formatStamp(data.window.start)} to ${formatStamp(data.window.end)}`;
      document.getElementById('sourceMeta').innerHTML = `
        <div>Scanned: ${escapeHtml(formatStamp(data.scanned_at))}</div>
        <div>Sessions dir: <span class="mono">${escapeHtml(data.sessions_dir)}</span></div>
        <div>Account matching: sessions directory ownership, otherwise shared bucket</div>
        <div>${data.sessions_dir_exists ? 'Directory found' : '<span class="error">Directory missing</span>'}</div>
      `;
    }

    function renderRecent(data){
      const rows = data.recent_sessions || [];
      document.getElementById('recentSessions').innerHTML = rows.length ? rows.map(row => `
        <div class="mini">
          <div class="title">${escapeHtml(row.project)}</div>
          <div class="meta">
            <span class="pill">${escapeHtml(tokenLine(row.window_usage))}</span>
            <div>${escapeHtml(row.label)}</div>
            <div>Last access: ${escapeHtml(formatStamp(row.last_access))}</div>
            <div class="mono">${escapeHtml(row.cwd || '-')}</div>
          </div>
        </div>
      `).join('') : `<div class="mini"><div class="meta">No recent sessions in the selected window.</div></div>`;
    }

    function renderProjects(data){
      const rows = data.projects || [];
      document.getElementById('projectsTable').innerHTML = `
        <thead>
          <tr>
            <th>Project</th>
            <th>Window Tokens</th>
            <th>Lifetime Tokens</th>
            <th>Sessions</th>
            <th>Last Access</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(row => `
            <tr>
              <td>
                <strong>${escapeHtml(row.project)}</strong>
                <span class="subline">${escapeHtml((row.cwd || []).slice(0,2).join(' | ') || '-')}</span>
                <span class="subline">Top session: ${escapeHtml(row.top_session || '-')}</span>
              </td>
              <td>
                ${escapeHtml(tokenLine(row.window_usage))}
                <span class="subline">${escapeHtml(tokenDetail(row.window_usage))}</span>
              </td>
              <td>
                ${escapeHtml(tokenLine(row.lifetime_usage))}
                <span class="subline">${escapeHtml(tokenDetail(row.lifetime_usage))}</span>
              </td>
              <td>${escapeHtml(String(row.session_count || 0))}</td>
              <td>${escapeHtml(formatStamp(row.last_access))}</td>
            </tr>
          `).join('')}
        </tbody>
      `;
    }

    function renderSessions(data){
      const rows = data.sessions || [];
      document.getElementById('sessionsTable').innerHTML = `
        <thead>
          <tr>
            <th>Session</th>
            <th>Project</th>
            <th>Window Tokens</th>
            <th>Lifetime Tokens</th>
            <th>Activity</th>
            <th>Last Access</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(row => `
            <tr>
              <td>
                <strong>${escapeHtml(row.label)}</strong>
                <span class="subline mono">${escapeHtml(row.file_path)}</span>
                <span class="subline">Started: ${escapeHtml(formatStamp(row.started_at))}</span>
              </td>
              <td>
                ${escapeHtml(row.project)}
                <span class="subline mono">${escapeHtml(row.cwd || '-')}</span>
                <span class="subline">${escapeHtml(row.originator || '-')} | ${escapeHtml(row.cli_version || '-')}</span>
              </td>
              <td>
                ${escapeHtml(tokenLine(row.window_usage))}
                <span class="subline">${escapeHtml(tokenDetail(row.window_usage))}</span>
              </td>
              <td>
                ${escapeHtml(tokenLine(row.lifetime_usage))}
                <span class="subline">${escapeHtml(tokenDetail(row.lifetime_usage))}</span>
              </td>
              <td>
                ${escapeHtml(row.user_messages)} user / ${escapeHtml(row.agent_messages)} agent
                <span class="subline">Turn contexts: ${escapeHtml(row.turn_contexts)} | Duration: ${escapeHtml(String(row.duration_seconds))}s</span>
                <span class="subline">Context window: ${escapeHtml(compact(row.context_window || 0))}</span>
                <span class="subline">Last user: ${escapeHtml(row.last_user_message || '-')}</span>
              </td>
              <td>${escapeHtml(formatStamp(row.last_access))}</td>
            </tr>
          `).join('')}
        </tbody>
      `;
    }

    function renderTimelineChart(data){
      const rows = [...(data.timeline || [])].reverse();
      if (!rows.length) {
        document.getElementById('timelineChart').innerHTML = '<div class="mini"><div class="meta">No token events in the selected window for this account.</div></div>';
        document.getElementById('timelineMeta').innerHTML = '';
        return;
      }
      const values = rows.map(row => Number((row.usage || {})[currentMetric] || 0));
      const width = 1080;
      const height = 320;
      const pad = {top: 24, right: 20, bottom: 46, left: 68};
      const innerWidth = width - pad.left - pad.right;
      const innerHeight = height - pad.top - pad.bottom;
      const maxValue = Math.max(...values, 0);
      const safeMax = maxValue > 0 ? maxValue : 1;
      const xFor = index => rows.length > 1 ? pad.left + (index * innerWidth) / (rows.length - 1) : pad.left + innerWidth / 2;
      const yFor = value => pad.top + innerHeight - (value / safeMax) * innerHeight;
      const linePath = rows.map((row, index) => {
        const value = Number((row.usage || {})[currentMetric] || 0);
        return `${index === 0 ? 'M' : 'L'} ${xFor(index).toFixed(2)} ${yFor(value).toFixed(2)}`;
      }).join(' ');
      const areaPath = `${linePath} L ${xFor(rows.length - 1).toFixed(2)} ${(pad.top + innerHeight).toFixed(2)} L ${xFor(0).toFixed(2)} ${(pad.top + innerHeight).toFixed(2)} Z`;
      const yTicks = [0, 0.25, 0.5, 0.75, 1].map(fraction => {
        const value = Math.round(safeMax * fraction);
        return {value, y: yFor(value)};
      });
      const labelEvery = rows.length > 16 ? Math.ceil(rows.length / 8) : 1;
      const xTicks = rows.map((row, index) => ({row, index})).filter(item => item.index % labelEvery === 0 || item.index === rows.length - 1);
      document.getElementById('timelineChart').innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Token usage timeline">
          <defs>
            <linearGradient id="areaFill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stop-color="#37c7a7" stop-opacity="0.34"></stop>
              <stop offset="100%" stop-color="#37c7a7" stop-opacity="0.04"></stop>
            </linearGradient>
          </defs>
          ${yTicks.map(tick => `
            <g>
              <line x1="${pad.left}" x2="${width - pad.right}" y1="${tick.y}" y2="${tick.y}" stroke="rgba(155,188,222,.14)"></line>
              <text x="${pad.left - 10}" y="${tick.y + 4}" fill="#9bb1c8" font-size="11" text-anchor="end">${escapeHtml(compact(tick.value))}</text>
            </g>
          `).join('')}
          <line x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${pad.top + innerHeight}" stroke="rgba(155,188,222,.25)"></line>
          <line x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + innerHeight}" y2="${pad.top + innerHeight}" stroke="rgba(155,188,222,.25)"></line>
          <path d="${areaPath}" fill="url(#areaFill)"></path>
          <path d="${linePath}" fill="none" stroke="#62a7ff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>
          ${rows.map((row, index) => {
            const value = Number((row.usage || {})[currentMetric] || 0);
            return `<circle cx="${xFor(index)}" cy="${yFor(value)}" r="3.5" fill="#37c7a7"></circle>`;
          }).join('')}
          ${xTicks.map(item => `
            <g>
              <line x1="${xFor(item.index)}" x2="${xFor(item.index)}" y1="${pad.top + innerHeight}" y2="${pad.top + innerHeight + 6}" stroke="rgba(155,188,222,.25)"></line>
              <text x="${xFor(item.index)}" y="${height - 10}" fill="#9bb1c8" font-size="11" text-anchor="middle">${escapeHtml(formatStamp(item.row.bucket_start, data.window.bucket === 'day' ? 'date' : 'datetime'))}</text>
            </g>
          `).join('')}
        </svg>
      `;
      document.getElementById('timelineMeta').innerHTML = `
        <div>Metric: ${escapeHtml(currentMetric.replaceAll('_', ' '))}</div>
        <div>Scale: 0 to ${escapeHtml(compact(maxValue))} tokens per ${escapeHtml(data.window.bucket)}</div>
        <div>Buckets: ${escapeHtml(String(rows.length))}</div>
      `;
    }

    function renderTimeline(data){
      const rows = data.timeline || [];
      renderTimelineChart(data);
      document.getElementById('timelineTable').innerHTML = `
        <thead>
          <tr>
            <th>${data.window.bucket === 'hour' ? 'Hour' : 'Day'}</th>
            <th>Tokens</th>
            <th>Projects</th>
            <th>Sessions</th>
            <th>Top Breakdown</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(row => `
            <tr>
              <td>${escapeHtml(formatStamp(row.bucket_start, data.window.bucket === 'day' ? 'date' : 'datetime'))}</td>
              <td>
                ${escapeHtml(tokenLine(row.usage))}
                <span class="subline">${escapeHtml(tokenDetail(row.usage))}</span>
              </td>
              <td>${escapeHtml(String(row.project_count || 0))}</td>
              <td>${escapeHtml(String(row.session_count || 0))}</td>
              <td>
                <span class="subline">Projects: ${escapeHtml((row.top_projects || []).map(item => `${item.name} (${compact(item.usage.total_tokens || 0)})`).join(' | ') || '-')}</span>
                <span class="subline">Sessions: ${escapeHtml((row.top_sessions || []).map(item => `${item.name} (${compact(item.usage.total_tokens || 0)})`).join(' | ') || '-')}</span>
              </td>
            </tr>
          `).join('')}
        </tbody>
      `;
    }

    async function loadSnapshot(){
      const params = readControls();
      const response = await fetch(`/api/snapshot?${params.toString()}`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || 'Request failed');
      }
      currentAccount = data.selected_account;
      renderAccountTabs(data);
      renderSummary(data);
      renderRecent(data);
      renderProjects(data);
      renderSessions(data);
      renderTimeline(data);
    }

    document.querySelectorAll('.tabs button[data-view]').forEach(button => {
      button.addEventListener('click', () => {
        const view = button.dataset.view;
        document.querySelectorAll('.tabs button[data-view]').forEach(item => item.classList.toggle('active', item === button));
        document.querySelectorAll('.view').forEach(panel => panel.classList.toggle('hidden', panel.dataset.view !== view));
      });
    });

    document.querySelectorAll('#metricTabs [data-metric]').forEach(button => {
      button.addEventListener('click', () => {
        currentMetric = button.dataset.metric;
        document.querySelectorAll('#metricTabs [data-metric]').forEach(item => item.classList.toggle('active', item === button));
        loadSnapshot().catch(err => alert(err.message));
      });
    });

    document.getElementById('refresh').addEventListener('click', () => {
      loadSnapshot().catch(err => alert(err.message));
    });
    document.getElementById('now').addEventListener('click', () => {
      setNowRange();
      loadSnapshot().catch(err => alert(err.message));
    });
    document.getElementById('preset').addEventListener('change', () => {
      const custom = document.getElementById('preset').value === 'custom';
      document.getElementById('start').disabled = !custom;
      document.getElementById('end').disabled = !custom;
    });

    setNowRange();
    document.getElementById('preset').dispatchEvent(new Event('change'));
    loadSnapshot().catch(err => {
      document.getElementById('summary').innerHTML = `<div class="stat"><div class="label">Error</div><div class="value error">${escapeHtml(err.message)}</div></div>`;
    });
  </script>
</body>
</html>
"""


def html_page_v2() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Sessions Dashboard</title>
  <style>
    :root{
      --bg:#071019;
      --bg2:#0b1522;
      --panel:#0f1b2c;
      --line:rgba(156,178,205,.15);
      --ink:#eef5ff;
      --muted:#96abc3;
      --accent:#64d2b1;
      --shadow:0 22px 60px rgba(0,0,0,.28);
      --radius:18px;
      --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
      --project-1:#69d0b0;
      --project-2:#79a7ff;
      --project-3:#ffb86a;
      --project-4:#e587ff;
      --project-5:#6fe0ff;
      --project-6:#ffd26e;
      --project-7:#ff8f8f;
      --project-8:#8cf08e;
      --project-9:#8db8ff;
      --project-10:#f6a0c8;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      color:var(--ink);
      font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:
        radial-gradient(circle at top left, rgba(100,210,177,.13), transparent 24%),
        radial-gradient(circle at top right, rgba(119,168,255,.12), transparent 20%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg2) 56%, #09111b 100%);
    }
    button,input,select{font:inherit}
    .topbar{
      position:sticky;top:0;z-index:10;
      backdrop-filter:blur(14px);
      background:rgba(7,16,25,.82);
      border-bottom:1px solid var(--line);
    }
    .topbar-inner{max-width:1680px;margin:0 auto;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;gap:16px}
    .brand{font-size:15px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}
    .brand small{display:block;font-size:12px;font-weight:600;letter-spacing:0;color:var(--muted);text-transform:none}
    .status{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px}
    .status-dot{width:9px;height:9px;border-radius:999px;background:var(--accent);box-shadow:0 0 0 4px rgba(100,210,177,.14)}
    .shell{max-width:1680px;margin:0 auto;padding:20px 22px 30px}
    .panel{
      background:linear-gradient(180deg, rgba(18,31,49,.96), rgba(10,19,31,.96));
      border:1px solid var(--line);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
    }
    .control-panel{padding:18px}
    .eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);font-weight:800}
    .hero-copy h1{margin:8px 0 0;font-size:30px;line-height:1.02;letter-spacing:-.03em}
    .hero-copy p{margin:10px 0 0;color:var(--muted);max-width:74ch}
    .account-tabs{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
    .account-tab{
      border:1px solid rgba(156,178,205,.18);background:#0a1420;color:var(--ink);
      border-radius:999px;padding:10px 14px;min-width:0;cursor:pointer;text-align:left;
    }
    .account-tab.active{background:var(--ink);color:#0a1420;border-color:var(--ink)}
    .account-tab .title{font-weight:800;display:block;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .account-tab .meta{display:block;font-size:11px;color:var(--muted);margin-top:2px}
    .account-tab.active .meta{color:rgba(10,20,32,.68)}
    .filters{margin-top:16px;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;align-items:end}
    .field{display:flex;flex-direction:column;gap:6px}
    .field label{font-size:11px;color:var(--muted);font-weight:800;letter-spacing:.08em;text-transform:uppercase}
    .field input,.field select{width:100%;padding:10px 12px;color:var(--ink);background:#09131f;border:1px solid rgba(156,178,205,.18);border-radius:10px}
    .range-strip{display:flex;gap:8px;flex-wrap:wrap}
    .chip-btn,.ghost-btn,.primary-btn,.tiny-btn,.tab-btn{
      border:1px solid rgba(156,178,205,.18);background:#0a1420;color:var(--ink);
      border-radius:999px;cursor:pointer;font-weight:800;
    }
    .chip-btn,.tab-btn{padding:9px 12px}
    .chip-btn.active,.tab-btn.active{background:var(--ink);color:#08101a;border-color:var(--ink)}
    .primary-btn{padding:10px 14px;background:linear-gradient(135deg, rgba(100,210,177,.26), rgba(119,168,255,.22))}
    .ghost-btn{padding:10px 14px}
    .tiny-btn{padding:6px 10px;font-size:12px}
    .metric-grid{margin-top:18px;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
    .metric{padding:14px;border-radius:14px;background:rgba(6,13,22,.44);border:1px solid rgba(156,178,205,.11);min-width:0}
    .metric .label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:800}
    .metric .value{margin-top:8px;font-size:24px;font-weight:800;letter-spacing:-.03em}
    .metric .meta{margin-top:6px;font-size:12px;color:var(--muted)}
    .workspace{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(340px,.9fr);gap:16px;margin-top:16px;align-items:start}
    .panel-head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
    .panel-head h2{margin:2px 0 0;font-size:20px}
    .view-tabs,.timeline-mode{display:flex;gap:8px;flex-wrap:wrap}
    .content-meta{padding:12px 18px 0;color:var(--muted);font-size:12px}
    .content-wrap{padding:14px 18px 18px}
    .table-shell{border:1px solid rgba(156,178,205,.1);border-radius:14px;overflow:auto;background:rgba(5,10,18,.42)}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{padding:11px 10px;text-align:left;border-bottom:1px solid rgba(156,178,205,.08);vertical-align:top}
    th{position:sticky;top:0;background:#102036;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;z-index:1}
    tbody tr{cursor:pointer}
    tbody tr:hover{background:rgba(119,168,255,.08)}
    tbody tr.active{background:rgba(100,210,177,.09)}
    .mono{font-family:var(--mono)}
    .subline{display:block;margin-top:4px;color:var(--muted);font-size:12px;line-height:1.35;overflow-wrap:anywhere}
    .num{white-space:nowrap}
    .actions{display:flex;gap:6px;flex-wrap:wrap}
    .path{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .state-pill{display:inline-flex;align-items:center;gap:6px;padding:5px 8px;border-radius:999px;background:rgba(119,168,255,.12);color:#d8e6ff;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
    .timeline-shell{border:1px solid rgba(156,178,205,.1);border-radius:16px;padding:12px 12px 6px;background:linear-gradient(180deg, rgba(7,13,22,.9), rgba(10,18,30,.9));overflow:auto}
    .timeline-chart{min-width:860px;height:300px;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(34px,1fr);gap:10px;align-items:end;padding:16px 6px 8px;border-bottom:1px solid rgba(156,178,205,.08)}
    .bucket{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:0}
    .bucket-btn{width:100%;height:220px;display:flex;align-items:flex-end;justify-content:center;border:none;background:transparent;padding:0;cursor:pointer}
    .bucket-stack{width:100%;max-width:42px;min-height:6px;border-radius:12px 12px 5px 5px;overflow:hidden;display:flex;flex-direction:column-reverse;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03);transition:transform .14s ease}
    .bucket-btn:hover .bucket-stack{transform:translateY(-2px)}
    .bucket.active .bucket-stack{outline:2px solid rgba(255,255,255,.22);outline-offset:4px}
    .segment{width:100%}
    .segment.output{background-image:repeating-linear-gradient(135deg, rgba(255,255,255,.28) 0 4px, transparent 4px 8px);background-blend-mode:screen;opacity:.94}
    .bucket-label,.bucket-total{font-size:11px;color:var(--muted);text-align:center;white-space:nowrap}
    .timeline-notes{padding:10px 4px 2px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}
    .legend{display:flex;gap:10px;flex-wrap:wrap}
    .legend-item{display:inline-flex;align-items:center;gap:6px}
    .swatch{width:10px;height:10px;border-radius:999px;display:inline-block}
    .detail-panel{padding:18px;position:sticky;top:74px}
    .detail-panel h3{margin:8px 0 0;font-size:24px;line-height:1.05;overflow-wrap:anywhere}
    .detail-panel p{color:var(--muted);overflow-wrap:anywhere}
    .chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
    .chip{display:inline-flex;align-items:center;gap:6px;padding:7px 10px;border-radius:999px;background:#0a1420;border:1px solid rgba(156,178,205,.12);font-size:12px;font-weight:800}
    .kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    .kv-item{padding:12px;border-radius:12px;border:1px solid rgba(156,178,205,.1);background:rgba(6,12,21,.46);min-width:0}
    .kv-item .k{font-size:11px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.08em}
    .kv-item .v{margin-top:5px;font-size:14px;font-weight:800;overflow-wrap:anywhere}
    .detail-section{margin-top:18px}
    .detail-section h4{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
    .stack-list{display:flex;flex-direction:column;gap:10px}
    .stack-item{padding:12px;border-radius:12px;border:1px solid rgba(156,178,205,.1);background:rgba(6,12,21,.46)}
    .stack-item .title{font-weight:800;overflow-wrap:anywhere}
    .stack-item .meta{margin-top:4px;color:var(--muted);font-size:12px;line-height:1.4;overflow-wrap:anywhere}
    .command{padding:10px 12px;border-radius:10px;background:#09131f;border:1px solid rgba(156,178,205,.12);color:#d8e6ff;font-family:var(--mono);font-size:12px;overflow:auto;white-space:nowrap}
    .empty{padding:24px;color:var(--muted)}
    .toast{position:fixed;right:20px;bottom:20px;padding:10px 14px;border-radius:999px;background:rgba(8,16,27,.94);border:1px solid rgba(156,178,205,.18);color:var(--ink);box-shadow:0 16px 32px rgba(0,0,0,.28);opacity:0;pointer-events:none;transform:translateY(8px);transition:opacity .18s ease, transform .18s ease;font-size:12px;font-weight:800}
    .toast.show{opacity:1;transform:translateY(0)}
    @media (max-width:1280px){
      .filters{grid-template-columns:repeat(3,minmax(0,1fr))}
      .metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}
      .workspace{grid-template-columns:1fr}
      .detail-panel{position:static}
    }
    @media (max-width:760px){
      .shell{padding:14px}
      .filters,.metric-grid,.kv{grid-template-columns:1fr}
      .hero-copy h1{font-size:24px}
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-inner">
      <div class="brand">Codex Sessions Dashboard<small>Dense local session analytics with account tabs and timeline drill-down</small></div>
      <div class="status"><span class="status-dot"></span><span id="navStatus">Loading local sessions...</span></div>
    </div>
  </div>
  <div class="shell">
    <section class="panel control-panel">
      <div class="hero-copy">
        <div class="eyebrow">Explorer</div>
        <h1>Sessions first, timeline second, account tabs always visible.</h1>
        <p>Codex sessions, project paths, resumable commands, and token usage are presented in a single dense workflow. Input includes cached input. Output includes reasoning output.</p>
      </div>
      <div class="account-tabs" id="accountTabs"></div>
      <div class="filters">
        <div class="field">
          <label>Quick Range</label>
          <div class="range-strip">
            <button class="chip-btn" data-preset="24h">24H</button>
            <button class="chip-btn" data-preset="5d">5D</button>
            <button class="chip-btn" data-preset="7d">7D</button>
            <button class="chip-btn" data-preset="30d">30D</button>
            <button class="chip-btn" data-preset="all">All</button>
          </div>
        </div>
        <div class="field">
          <label for="bucketMode">Timeline Bucket</label>
          <select id="bucketMode"><option value="day">Day</option><option value="hour">Hour</option></select>
        </div>
        <div class="field"><label for="rangeStart">Custom Start</label><input id="rangeStart" type="datetime-local"></div>
        <div class="field"><label for="rangeEnd">Custom End</label><input id="rangeEnd" type="datetime-local"></div>
        <div class="field"><label for="search">Search</label><input id="search" type="search" placeholder="project, path, session, message"></div>
        <div class="field">
          <label>Actions</label>
          <div class="range-strip"><button class="primary-btn" id="refreshBtn">Refresh</button><button class="ghost-btn" id="customBtn">Use Custom</button></div>
        </div>
        <div class="field"><label>Window</label><div class="subline" id="windowMeta" style="margin-top:0">-</div></div>
      </div>
      <div class="metric-grid" id="metrics"></div>
    </section>
    <section class="workspace">
      <div class="panel">
        <div class="panel-head">
          <div><div class="eyebrow">Views</div><h2 id="viewTitle">Sessions</h2></div>
          <div class="view-tabs">
            <button class="tab-btn active" data-view="sessions">Sessions</button>
            <button class="tab-btn" data-view="projects">Projects</button>
            <button class="tab-btn" data-view="timeline">Timeline</button>
          </div>
        </div>
        <div class="content-meta" id="contentMeta"></div>
        <div class="content-wrap" id="mainContent"></div>
      </div>
      <aside class="panel detail-panel" id="detailPanel"></aside>
    </section>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    const fmtInt = new Intl.NumberFormat();
    const fmtDateTime = new Intl.DateTimeFormat([], {dateStyle:'medium', timeStyle:'short'});
    const fmtDateOnly = new Intl.DateTimeFormat([], {dateStyle:'medium'});
    const projectPalette = ['var(--project-1)','var(--project-2)','var(--project-3)','var(--project-4)','var(--project-5)','var(--project-6)','var(--project-7)','var(--project-8)','var(--project-9)','var(--project-10)'];
    const state = {snapshot:null, selectedAccount:'all', preset:'7d', view:'sessions', selectedSessionId:null, selectedProjectId:null, selectedBucketStart:null, timelineMode:'sessions', search:''};
    function el(id){ return document.getElementById(id); }
    function compact(value){ const n = Number(value || 0); const abs = Math.abs(n); if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(abs >= 10_000_000_000 ? 0 : 1)}B`; if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`; if (abs >= 1_000) return `${(n / 1_000).toFixed(abs >= 10_000 ? 0 : 1)}K`; return fmtInt.format(n); }
    function escapeHtml(value){ return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;'); }
    function formatStamp(value, kind='datetime'){ if (!value) return '-'; const date = new Date(value); if (Number.isNaN(date.getTime())) return '-'; return kind === 'date' ? fmtDateOnly.format(date) : fmtDateTime.format(date); }
    function localInputValue(value){ if (!value) return ''; const date = new Date(value); if (Number.isNaN(date.getTime())) return ''; const y=date.getFullYear(); const m=String(date.getMonth()+1).padStart(2,'0'); const d=String(date.getDate()).padStart(2,'0'); const h=String(date.getHours()).padStart(2,'0'); const mm=String(date.getMinutes()).padStart(2,'0'); return `${y}-${m}-${d}T${h}:${mm}`; }
    function toIsoLocal(value){ if (!value) return ''; const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toISOString(); }
    function inputTokens(usage){ return Number((usage || {}).input_tokens || 0) + Number((usage || {}).cached_input_tokens || 0); }
    function outputTokens(usage){ return Number((usage || {}).output_tokens || 0) + Number((usage || {}).reasoning_output_tokens || 0); }
    function totalTokens(usage){ return Number((usage || {}).total_tokens || 0) || inputTokens(usage) + outputTokens(usage); }
    function usageLine(usage){ return `${compact(inputTokens(usage))} in | ${compact(outputTokens(usage))} out`; }
    function usageMeta(usage){ const parts = []; if (Number((usage || {}).cached_input_tokens || 0)) parts.push(`cached ${compact((usage || {}).cached_input_tokens || 0)}`); if (Number((usage || {}).reasoning_output_tokens || 0)) parts.push(`reason ${compact((usage || {}).reasoning_output_tokens || 0)}`); parts.push(`total ${compact(totalTokens(usage))}`); return parts.join(' | '); }
    function projectColor(name){ let hash = 0; for (const ch of String(name || 'unknown')) hash = ((hash << 5) - hash) + ch.charCodeAt(0); return projectPalette[Math.abs(hash) % projectPalette.length]; }
    function showToast(message){ el('toast').textContent = message; el('toast').classList.add('show'); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => el('toast').classList.remove('show'), 1800); }
    async function copyText(value, label){ try { await navigator.clipboard.writeText(value); showToast(`${label} copied`); } catch (err) { showToast('Clipboard unavailable'); } }
    function queryParams(){ const params = new URLSearchParams(); params.set('preset', state.preset); params.set('bucket', el('bucketMode').value); params.set('account', state.selectedAccount || 'all'); if (state.preset === 'custom'){ const start = toIsoLocal(el('rangeStart').value); const end = toIsoLocal(el('rangeEnd').value); if (start) params.set('start', start); if (end) params.set('end', end); } return params; }
    async function loadSnapshot(){ el('navStatus').textContent = 'Refreshing local Codex session data...'; const res = await fetch(`/api/snapshot?${queryParams().toString()}`); const data = await res.json(); if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`); state.snapshot = data; state.selectedAccount = data.selected_account || state.selectedAccount || 'all'; syncControls(data); ensureSelection(); renderAll(); el('navStatus').textContent = `${formatStamp(data.scanned_at)} | ${data.sessions_dir_exists ? 'sessions available' : 'sessions directory missing'}`; }
    function syncControls(data){ el('bucketMode').value = data.window?.bucket || 'day'; document.querySelectorAll('[data-preset]').forEach(btn => btn.classList.toggle('active', btn.dataset.preset === state.preset)); el('rangeStart').value = localInputValue(data.window?.start); el('rangeEnd').value = localInputValue(data.window?.end); el('search').value = state.search; }
    function accountRows(){ return state.snapshot?.accounts || []; }
    function filteredSessions(){ const q = state.search.trim().toLowerCase(); let rows = [...(state.snapshot?.sessions || [])]; if (q) rows = rows.filter(row => `${row.label} ${row.project} ${row.cwd || ''} ${row.working_path || ''} ${row.last_user_message || ''} ${row.last_agent_message || ''}`.toLowerCase().includes(q)); return rows.sort((a,b) => new Date(b.last_access || 0).getTime() - new Date(a.last_access || 0).getTime() || totalTokens(b.window_usage) - totalTokens(a.window_usage)); }
    function projectId(row){ return `${row.project}::${(row.cwd || []).join('|')}`; }
    function filteredProjects(){ const q = state.search.trim().toLowerCase(); let rows = [...(state.snapshot?.projects || [])]; if (q) rows = rows.filter(row => `${row.project} ${(row.cwd || []).join(' ')} ${row.top_session || ''}`.toLowerCase().includes(q)); return rows.sort((a,b) => new Date(b.last_access || 0).getTime() - new Date(a.last_access || 0).getTime() || totalTokens(b.window_usage) - totalTokens(a.window_usage)); }
    function timelineRows(){ return [...(state.snapshot?.timeline || [])].reverse(); }
    function currentBucket(){ return timelineRows().find(row => row.bucket_start === state.selectedBucketStart) || timelineRows()[0] || null; }
    function timelineBreakdownRows(){ const bucket = currentBucket(); if (!bucket) return []; const q = state.search.trim().toLowerCase(); let rows = state.timelineMode === 'projects' ? [...(bucket.project_rows || [])] : [...(bucket.session_rows || [])]; if (q) rows = rows.filter(row => `${row.project || ''} ${row.label || ''} ${row.cwd || ''} ${row.session_id || ''}`.toLowerCase().includes(q)); return rows; }
    function ensureSelection(){ const sessions = filteredSessions(); if (!sessions.find(row => row.session_id === state.selectedSessionId)) state.selectedSessionId = sessions[0]?.session_id || null; const projects = filteredProjects(); if (!projects.find(row => projectId(row) === state.selectedProjectId)) state.selectedProjectId = projects[0] ? projectId(projects[0]) : null; const buckets = timelineRows(); if (!buckets.find(row => row.bucket_start === state.selectedBucketStart)) state.selectedBucketStart = buckets[buckets.length - 1]?.bucket_start || null; }
    function renderAccountTabs(){ el('accountTabs').innerHTML = accountRows().map(row => `<button class="account-tab ${row.key === state.selectedAccount ? 'active' : ''}" data-account="${escapeHtml(row.key)}"><span class="title">${escapeHtml(row.label)}</span><span class="meta">${compact(inputTokens(row.window_usage))} in | ${compact(outputTokens(row.window_usage))} out | ${fmtInt.format(row.window_session_count || row.session_count || 0)} sessions</span></button>`).join(''); }
    function renderMetrics(){ const summary = state.snapshot?.summary || {}; const totals = summary.totals || {}; const account = accountRows().find(row => row.key === state.selectedAccount); const cards = [['Input', compact(inputTokens(totals)), `Cached ${compact(totals.cached_input_tokens || 0)}`], ['Output', compact(outputTokens(totals)), `Reasoning ${compact(totals.reasoning_output_tokens || 0)}`], ['Sessions', fmtInt.format(summary.session_count || 0), `${fmtInt.format(summary.project_count || 0)} projects`], ['Latest', formatStamp(summary.latest_access), `Window bucket ${state.snapshot?.window?.bucket || '-'}`], ['Account', account ? account.label : 'All Accounts', `${compact(totalTokens((account || {}).window_usage || totals))} total`], ['Source', state.snapshot?.sessions_dir_exists ? 'Ready' : 'Missing', state.snapshot?.sessions_dir || '-']]; el('metrics').innerHTML = cards.map(([label, value, meta]) => `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div><div class="meta">${escapeHtml(meta)}</div></div>`).join(''); el('windowMeta').textContent = `${formatStamp(state.snapshot?.window?.start)} -> ${formatStamp(state.snapshot?.window?.end)} (${state.snapshot?.window?.preset || state.preset})`; }
    function renderSessionsView(){ const rows = filteredSessions(); el('viewTitle').textContent = 'Sessions'; el('contentMeta').textContent = `${fmtInt.format(rows.length)} sessions in scope. Click a row for details. Resume and path actions copy commands.`; if (!rows.length) { el('mainContent').innerHTML = '<div class="empty">No sessions matched the current account, window, and search filters.</div>'; return; } el('mainContent').innerHTML = `<div class="table-shell"><table><thead><tr><th>Session</th><th>Project / Path</th><th>Last Used</th><th>Input</th><th>Output</th><th>Total</th><th>Resume</th><th>Status</th></tr></thead><tbody>${rows.map(row => `<tr class="${row.session_id === state.selectedSessionId ? 'active' : ''}" data-select-session="${escapeHtml(row.session_id)}"><td><strong>${escapeHtml(row.label)}</strong><span class="subline mono">${escapeHtml(row.session_id)}</span><span class="subline">${escapeHtml(formatStamp(row.started_at))}</span></td><td><strong>${escapeHtml(row.project)}</strong><span class="subline mono path">${escapeHtml(row.cwd || row.working_path || '-')}</span><span class="subline">${escapeHtml(row.originator || '-')} | ${escapeHtml(row.cli_version || '-')}</span></td><td>${escapeHtml(formatStamp(row.last_access))}<span class="subline">${escapeHtml(row.user_messages)} user | ${escapeHtml(row.agent_messages)} agent | ${escapeHtml(String(row.duration_seconds || 0))}s</span></td><td class="num">${compact(inputTokens(row.window_usage))}<span class="subline">${compact(row.window_usage?.cached_input_tokens || 0)} cached</span></td><td class="num">${compact(outputTokens(row.window_usage))}<span class="subline">${compact(row.window_usage?.reasoning_output_tokens || 0)} reasoning</span></td><td class="num">${compact(totalTokens(row.window_usage))}<span class="subline">${escapeHtml(usageMeta(row.lifetime_usage))}</span></td><td><div class="actions"><button class="tiny-btn" data-copy-label="Resume command" data-copy="${escapeHtml(row.resume_command || '')}">Resume</button><button class="tiny-btn" data-copy-label="Path" data-copy="${escapeHtml(row.working_path || row.cwd || '')}">Path</button>${row.open_command ? `<button class="tiny-btn" data-copy-label="Open command" data-copy="${escapeHtml(row.open_command)}">Open</button>` : ''}</div></td><td><span class="state-pill">${escapeHtml(row.model_provider || 'codex')}</span><span class="subline">turns ${escapeHtml(String(row.turn_contexts || 0))} | ctx ${compact(row.context_window || 0)}</span></td></tr>`).join('')}</tbody></table></div>`; }
    function renderProjectsView(){ const rows = filteredProjects(); el('viewTitle').textContent = 'Projects'; el('contentMeta').textContent = `${fmtInt.format(rows.length)} projects in scope. Project rows aggregate the selected account and time window.`; if (!rows.length) { el('mainContent').innerHTML = '<div class="empty">No projects matched the current filters.</div>'; return; } el('mainContent').innerHTML = `<div class="table-shell"><table><thead><tr><th>Project</th><th>Paths</th><th>Sessions</th><th>Last Used</th><th>Input</th><th>Output</th><th>Total</th></tr></thead><tbody>${rows.map(row => `<tr class="${projectId(row) === state.selectedProjectId ? 'active' : ''}" data-select-project="${escapeHtml(projectId(row))}"><td><strong>${escapeHtml(row.project)}</strong><span class="subline">Top session: ${escapeHtml(row.top_session || '-')}</span></td><td><span class="subline mono">${escapeHtml((row.cwd || []).join(' | ') || '-')}</span></td><td class="num">${fmtInt.format(row.session_count || 0)}</td><td>${escapeHtml(formatStamp(row.last_access))}</td><td class="num">${compact(inputTokens(row.window_usage))}</td><td class="num">${compact(outputTokens(row.window_usage))}</td><td class="num">${compact(totalTokens(row.window_usage))}<span class="subline">${escapeHtml(usageMeta(row.lifetime_usage))}</span></td></tr>`).join('')}</tbody></table></div>`; }
    function bucketSegments(row){ const segments = []; for (const item of row.project_rows || []) { const inTokens = inputTokens(item.usage); const outTokens = outputTokens(item.usage); if (inTokens) segments.push({project:item.project, tokenType:'input', tokens:inTokens, color:projectColor(item.project)}); if (outTokens) segments.push({project:item.project, tokenType:'output', tokens:outTokens, color:projectColor(item.project)}); } return segments; }
    function renderTimelineView(){ const rows = timelineRows(); el('viewTitle').textContent = 'Timeline'; if (!rows.length) { el('contentMeta').textContent = 'No bucketed activity exists in the selected window.'; el('mainContent').innerHTML = '<div class="empty">No token activity landed in the selected range.</div>'; return; } const current = currentBucket(); const maxTotal = Math.max(...rows.map(row => totalTokens(row.usage)), 1); const breakdown = timelineBreakdownRows(); const legendProjects = [...new Set(rows.flatMap(row => (row.project_rows || []).slice(0, 3).map(item => item.project)).filter(Boolean))].slice(0, 6); el('contentMeta').textContent = `${fmtInt.format(rows.length)} ${state.snapshot?.window?.bucket || 'day'} buckets on the x-axis. Click a bucket to inspect sessions or projects behind that time slice.`; el('mainContent').innerHTML = `<div class="timeline-shell"><div class="timeline-chart">${rows.map(row => { const total = totalTokens(row.usage); const scaledHeight = Math.max(6, Math.round((total / maxTotal) * 220)); const segments = bucketSegments(row); return `<div class="bucket ${row.bucket_start === state.selectedBucketStart ? 'active' : ''}"><div class="bucket-total">${escapeHtml(compact(total))}</div><button class="bucket-btn" data-select-bucket="${escapeHtml(row.bucket_start)}" title="${escapeHtml(formatStamp(row.bucket_start, state.snapshot?.window?.bucket === 'day' ? 'date' : 'datetime'))}: ${escapeHtml(usageLine(row.usage))}"><div class="bucket-stack" style="height:${scaledHeight}px">${segments.map(segment => `<div class="segment ${segment.tokenType === 'output' ? 'output' : ''}" style="height:${Math.max(2, (segment.tokens / Math.max(total,1)) * 100)}%; background:${segment.color}" title="${escapeHtml(segment.project)} | ${segment.tokenType} | ${escapeHtml(compact(segment.tokens))}"></div>`).join('')}</div></button><div class="bucket-label">${escapeHtml(formatStamp(row.bucket_start, state.snapshot?.window?.bucket === 'day' ? 'date' : 'datetime'))}</div></div>`; }).join('')}</div><div class="timeline-notes"><div class="legend"><span class="legend-item"><span class="swatch" style="background:#bfe7db"></span>Input segments</span><span class="legend-item"><span class="swatch" style="background:#bfe7db;background-image:repeating-linear-gradient(135deg, rgba(255,255,255,.75) 0 4px, transparent 4px 8px)"></span>Output segments</span>${legendProjects.map(project => `<span class="legend-item"><span class="swatch" style="background:${projectColor(project)}"></span>${escapeHtml(project)}</span>`).join('')}</div><div>${escapeHtml(formatStamp(current?.bucket_start, state.snapshot?.window?.bucket === 'day' ? 'date' : 'datetime'))} | ${escapeHtml(usageLine((current || {}).usage || {}))}</div></div></div><div style="height:12px"></div><div class="panel" style="background:rgba(0,0,0,.08);box-shadow:none"><div class="panel-head" style="padding:12px 14px"><div><div class="eyebrow">Bucket Breakdown</div><h2 style="font-size:16px;margin-top:4px">${escapeHtml(formatStamp(current?.bucket_start, state.snapshot?.window?.bucket === 'day' ? 'date' : 'datetime'))}</h2></div><div class="timeline-mode"><button class="tab-btn ${state.timelineMode === 'sessions' ? 'active' : ''}" data-timeline-mode="sessions">Sessions</button><button class="tab-btn ${state.timelineMode === 'projects' ? 'active' : ''}" data-timeline-mode="projects">Projects</button></div></div><div class="content-wrap"><div class="table-shell"><table><thead><tr><th>${state.timelineMode === 'projects' ? 'Project' : 'Session'}</th><th>Path</th><th>Input</th><th>Output</th><th>Total</th><th>Jump</th></tr></thead><tbody>${breakdown.map(row => `<tr ${state.timelineMode === 'projects' ? `data-jump-project="${escapeHtml(row.project || '')}"` : `data-jump-session="${escapeHtml(row.session_id || '')}"`}><td><strong>${escapeHtml(row.project || row.label || '-')}</strong>${state.timelineMode === 'projects' ? '' : `<span class="subline mono">${escapeHtml(row.session_id || '-')}</span>`}</td><td><span class="subline mono">${escapeHtml(row.cwd || row.project || '-')}</span></td><td class="num">${compact(inputTokens(row.usage))}</td><td class="num">${compact(outputTokens(row.usage))}</td><td class="num">${compact(totalTokens(row.usage))}</td><td>${state.timelineMode === 'projects' ? 'Open project' : 'Open session'}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">No rows matched the search filter for this bucket.</td></tr>'}</tbody></table></div></div></div>`; }
    function renderDetail(){ if (state.view === 'projects') { const project = filteredProjects().find(row => projectId(row) === state.selectedProjectId); if (!project) { el('detailPanel').innerHTML = '<div class="eyebrow">Project Detail</div><h3>No project selected</h3><p>Choose a project row to inspect the recent sessions and token totals behind it.</p>'; return; } const related = filteredSessions().filter(row => row.project === project.project).slice(0, 8); el('detailPanel').innerHTML = `<div class="eyebrow">Project Detail</div><h3>${escapeHtml(project.project)}</h3><p>${escapeHtml((project.cwd || []).join(' | ') || '-')}</p><div class="chips"><span class="chip">${fmtInt.format(project.session_count || 0)} sessions</span><span class="chip">${compact(inputTokens(project.window_usage))} in</span><span class="chip">${compact(outputTokens(project.window_usage))} out</span><span class="chip">${compact(totalTokens(project.window_usage))} total</span></div><div class="kv"><div class="kv-item"><div class="k">Last Used</div><div class="v">${escapeHtml(formatStamp(project.last_access))}</div></div><div class="kv-item"><div class="k">Top Session</div><div class="v">${escapeHtml(project.top_session || '-')}</div></div><div class="kv-item"><div class="k">Lifetime Input</div><div class="v">${compact(inputTokens(project.lifetime_usage))}</div></div><div class="kv-item"><div class="k">Lifetime Output</div><div class="v">${compact(outputTokens(project.lifetime_usage))}</div></div></div><div class="detail-section"><h4>Recent Sessions</h4><div class="stack-list">${related.map(row => `<div class="stack-item"><div class="title">${escapeHtml(row.label)}</div><div class="meta">${escapeHtml(formatStamp(row.last_access))} | ${escapeHtml(usageLine(row.window_usage))}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No related sessions in this view.</div></div>'}</div></div>`; return; } if (state.view === 'timeline') { const bucket = currentBucket(); if (!bucket) { el('detailPanel').innerHTML = '<div class="eyebrow">Timeline Detail</div><h3>No bucket selected</h3><p>Select a bucket in the chart to inspect that day or hour.</p>'; return; } el('detailPanel').innerHTML = `<div class="eyebrow">Timeline Detail</div><h3>${escapeHtml(formatStamp(bucket.bucket_start, state.snapshot?.window?.bucket === 'day' ? 'date' : 'datetime'))}</h3><p>${escapeHtml((state.snapshot?.selected_account || 'all'))} scope | ${fmtInt.format(bucket.session_count || 0)} sessions | ${fmtInt.format(bucket.project_count || 0)} projects</p><div class="chips"><span class="chip">${compact(inputTokens(bucket.usage))} in</span><span class="chip">${compact(outputTokens(bucket.usage))} out</span><span class="chip">${compact(totalTokens(bucket.usage))} total</span></div><div class="detail-section"><h4>Top Projects</h4><div class="stack-list">${(bucket.project_rows || []).slice(0, 6).map(row => `<div class="stack-item"><div class="title">${escapeHtml(row.project)}</div><div class="meta">${escapeHtml(usageLine(row.usage))} | ${escapeHtml(usageMeta(row.usage))}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No project rows for this bucket.</div></div>'}</div></div><div class="detail-section"><h4>Top Sessions</h4><div class="stack-list">${(bucket.session_rows || []).slice(0, 6).map(row => `<div class="stack-item"><div class="title">${escapeHtml(row.label || row.session_id)}</div><div class="meta">${escapeHtml(row.project || '-')} | ${escapeHtml(usageLine(row.usage))}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No session rows for this bucket.</div></div>'}</div></div>`; return; } const session = filteredSessions().find(row => row.session_id === state.selectedSessionId); if (!session) { el('detailPanel').innerHTML = '<div class="eyebrow">Session Detail</div><h3>No session selected</h3><p>Select a session to inspect path, resume command, and token usage.</p>'; return; } el('detailPanel').innerHTML = `<div class="eyebrow">Session Detail</div><h3>${escapeHtml(session.label)}</h3><p>${escapeHtml(session.project)} | ${escapeHtml(formatStamp(session.last_access))}</p><div class="chips"><span class="chip">${compact(inputTokens(session.window_usage))} in</span><span class="chip">${compact(outputTokens(session.window_usage))} out</span><span class="chip">${compact(totalTokens(session.window_usage))} total</span><span class="chip">ctx ${compact(session.context_window || 0)}</span></div><div class="kv"><div class="kv-item"><div class="k">Session ID</div><div class="v mono">${escapeHtml(session.session_id)}</div></div><div class="kv-item"><div class="k">Working Path</div><div class="v mono">${escapeHtml(session.working_path || session.cwd || '-')}</div></div><div class="kv-item"><div class="k">Started</div><div class="v">${escapeHtml(formatStamp(session.started_at))}</div></div><div class="kv-item"><div class="k">Duration</div><div class="v">${escapeHtml(String(session.duration_seconds || 0))}s</div></div><div class="kv-item"><div class="k">User / Agent</div><div class="v">${escapeHtml(String(session.user_messages || 0))} / ${escapeHtml(String(session.agent_messages || 0))}</div></div><div class="kv-item"><div class="k">Lifetime</div><div class="v">${escapeHtml(usageLine(session.lifetime_usage))}</div></div></div><div class="detail-section"><h4>Resume Command</h4><div class="command">${escapeHtml(session.resume_command || '-')}</div><div style="height:8px"></div><div class="actions"><button class="tiny-btn" data-copy-label="Resume command" data-copy="${escapeHtml(session.resume_command || '')}">Copy Resume</button><button class="tiny-btn" data-copy-label="Path" data-copy="${escapeHtml(session.working_path || session.cwd || '')}">Copy Path</button>${session.open_command ? `<button class="tiny-btn" data-copy-label="Open command" data-copy="${escapeHtml(session.open_command)}">Copy Open</button>` : ''}</div></div><div class="detail-section"><h4>Recent Messages</h4><div class="stack-list"><div class="stack-item"><div class="title">Last User</div><div class="meta">${escapeHtml(session.last_user_message || '-')}</div></div><div class="stack-item"><div class="title">Last Agent</div><div class="meta">${escapeHtml(session.last_agent_message || '-')}</div></div></div></div><div class="detail-section"><h4>Files</h4><div class="stack-list"><div class="stack-item"><div class="title">Session Log</div><div class="meta mono">${escapeHtml(session.file_path || '-')}</div></div></div></div>`; }
    function renderMain(){ if (state.view === 'projects') renderProjectsView(); else if (state.view === 'timeline') renderTimelineView(); else renderSessionsView(); }
    function renderAll(){ renderAccountTabs(); renderMetrics(); renderMain(); renderDetail(); }
    document.body.addEventListener('click', async (event) => {
      const copyBtn = event.target.closest('[data-copy]'); if (copyBtn) { event.stopPropagation(); await copyText(copyBtn.dataset.copy || '', copyBtn.dataset.copyLabel || 'Value'); return; }
      const accountBtn = event.target.closest('[data-account]'); if (accountBtn) { state.selectedAccount = accountBtn.dataset.account; await loadSnapshot(); return; }
      const presetBtn = event.target.closest('[data-preset]'); if (presetBtn) { state.preset = presetBtn.dataset.preset; await loadSnapshot(); return; }
      const viewBtn = event.target.closest('[data-view]'); if (viewBtn) { state.view = viewBtn.dataset.view; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); ensureSelection(); renderMain(); renderDetail(); return; }
      const bucketBtn = event.target.closest('[data-select-bucket]'); if (bucketBtn) { state.selectedBucketStart = bucketBtn.dataset.selectBucket; renderMain(); renderDetail(); return; }
      const sessionBtn = event.target.closest('[data-select-session]'); if (sessionBtn) { state.selectedSessionId = sessionBtn.dataset.selectSession; renderMain(); renderDetail(); return; }
      const projectBtn = event.target.closest('[data-select-project]'); if (projectBtn) { state.selectedProjectId = projectBtn.dataset.selectProject; renderMain(); renderDetail(); return; }
      const jumpSession = event.target.closest('[data-jump-session]'); if (jumpSession) { state.view = 'sessions'; state.selectedSessionId = jumpSession.dataset.jumpSession; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); renderMain(); renderDetail(); return; }
      const jumpProject = event.target.closest('[data-jump-project]'); if (jumpProject) { const project = filteredProjects().find(row => row.project === jumpProject.dataset.jumpProject); state.view = 'projects'; state.selectedProjectId = project ? projectId(project) : state.selectedProjectId; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); renderMain(); renderDetail(); return; }
      const timelineModeBtn = event.target.closest('[data-timeline-mode]'); if (timelineModeBtn) { state.timelineMode = timelineModeBtn.dataset.timelineMode; renderMain(); renderDetail(); }
    });
    el('refreshBtn').addEventListener('click', () => loadSnapshot().catch(err => showToast(err.message)));
    el('customBtn').addEventListener('click', () => { state.preset = 'custom'; if (!el('rangeStart').value || !el('rangeEnd').value) { const end = new Date(); const start = new Date(end.getTime() - (7 * 24 * 3600 * 1000)); el('rangeStart').value = localInputValue(start.toISOString()); el('rangeEnd').value = localInputValue(end.toISOString()); } loadSnapshot().catch(err => showToast(err.message)); });
    el('bucketMode').addEventListener('change', () => loadSnapshot().catch(err => showToast(err.message)));
    el('search').addEventListener('input', (event) => { state.search = event.target.value || ''; ensureSelection(); renderMain(); renderDetail(); });
    loadSnapshot().catch(err => { el('navStatus').textContent = 'Load failed'; el('mainContent').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`; el('detailPanel').innerHTML = `<div class="eyebrow">Status</div><h3>Unable to load dashboard</h3><p>${escapeHtml(err.message)}</p>`; });
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    app: CodexSessionApp

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(200, html_page_v2())
            return
        if parsed.path == "/api/snapshot":
            params = parse_qs(parsed.query)
            try:
                payload = self.app.build_snapshot(
                    preset=(params.get("preset") or [DEFAULT_PRESET])[0],
                    bucket=(params.get("bucket") or ["day"])[0],
                    start=(params.get("start") or [None])[0],
                    end=(params.get("end") or [None])[0],
                    account=(params.get("account") or [None])[0],
                )
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    args = parse_args()
    app = CodexSessionApp(args.sessions_dir, args.config)
    handler = type("CodexSessionDashboardHandler", (DashboardHandler,), {"app": app})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Codex Sessions Dashboard listening on {url}")
    print(f"Reading sessions from {args.sessions_dir.expanduser()}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
