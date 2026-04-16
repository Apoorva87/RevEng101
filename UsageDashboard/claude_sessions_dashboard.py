#!/usr/bin/env python3
"""Local dashboard for inspecting Claude Code session data under ~/.claude."""

from __future__ import annotations

import argparse
import json
import shlex
import threading
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
DEFAULT_ROOT = Path.home() / ".claude"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8876

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Claude Code session analytics dashboard.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"Claude root directory. Default: {DEFAULT_ROOT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host. Default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port. Default: {DEFAULT_PORT}")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open a browser.")
    return parser.parse_args()


def compact_error(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def parse_iso_ts(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ts_to_local(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)


def format_bucket(ts: float | None, mode: str) -> str:
    dt = ts_to_local(ts)
    if dt is None:
        return "-"
    if mode == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    return dt.strftime("%Y-%m-%d")


def format_relative(ts: float | None) -> str:
    if ts is None:
        return "-"
    delta = datetime.now(tz=LOCAL_TZ) - datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def resume_command_for(session_id: str, project_path: str | None) -> str:
    if project_path:
        return f"cd {shell_quote(project_path)} && claude --resume {shell_quote(session_id)}"
    return f"claude --resume {shell_quote(session_id)}"


def open_command_for(path: str | None) -> str | None:
    if not path:
        return None
    return f"open {shell_quote(path)}"


def local_day_start(ts: float) -> datetime:
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def window_total(rows: list[dict[str, Any]], days: int, field: str = "total_tokens") -> int:
    if not rows:
        return 0
    latest = max((row["bucket_start"] for row in rows), default=0)
    if not latest:
        return 0
    cutoff = local_day_start(latest) - timedelta(days=days - 1)
    cutoff_ts = cutoff.timestamp()
    return sum(int(row.get(field, 0) or 0) for row in rows if row.get("bucket_start", 0) >= cutoff_ts)


def safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def numeric_usage_totals(usage: dict[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for field in USAGE_FIELDS:
        totals[field] = int(usage.get(field) or 0)
    server_tool_use = usage.get("server_tool_use") or {}
    totals["web_search_requests"] = int(server_tool_use.get("web_search_requests") or 0)
    totals["read_tokens"] = (
        totals["input_tokens"] + totals["cache_read_input_tokens"] + totals["cache_creation_input_tokens"]
    )
    totals["write_tokens"] = totals["output_tokens"]
    totals["total_tokens"] = totals["read_tokens"] + totals["write_tokens"]
    return totals


def merge_counter(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = int(target.get(key, 0) or 0) + int(value or 0)


def is_tool_result_only(content: Any) -> bool:
    if not isinstance(content, list) or not content:
        return False
    item_types = {item.get("type") for item in content if isinstance(item, dict)}
    return item_types == {"tool_result"}


def extract_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            texts.append(str(item["text"]).strip())
    merged = " ".join(text for text in texts if text)
    return merged or None


def extract_error_text(entry: dict[str, Any]) -> str | None:
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                return str(item["text"]).strip()
    if isinstance(content, str):
        return content.strip()
    tool_use_result = entry.get("toolUseResult")
    if tool_use_result:
        return str(tool_use_result).strip()
    return None


@dataclass
class SessionParseResult:
    summary: dict[str, Any]
    day_rows: list[dict[str, Any]]
    hour_rows: list[dict[str, Any]]
    usage_events: list[dict[str, Any]]
    limit_events: list[dict[str, Any]]


class ClaudeSessionAnalyzer:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.expanduser()
        self.projects_dir = self.root_dir / "projects"
        self.history_path = self.root_dir / "history.jsonl"
        self.stats_cache_path = self.root_dir / "stats-cache.json"
        self.settings_path = self.root_dir / "settings.json"
        self.telemetry_dir = self.root_dir / "telemetry"
        self._session_cache: dict[str, tuple[tuple[int, int], SessionParseResult]] = {}
        self._history_cache: tuple[tuple[int, int] | None, dict[str, dict[str, Any]]] = (None, {})
        self._telemetry_cache: tuple[tuple[tuple[str, int, int], ...], dict[str, dict[str, Any]]] = ((), {})

    def _history_signature(self) -> tuple[int, int] | None:
        if not self.history_path.exists():
            return None
        stat = self.history_path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _scan_history(self) -> dict[str, dict[str, Any]]:
        signature = self._history_signature()
        if signature == self._history_cache[0]:
            return self._history_cache[1]

        result: dict[str, dict[str, Any]] = {}
        if self.history_path.exists():
            for raw_line in self.history_path.read_text(encoding="utf-8", errors="replace").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                session_id = entry.get("sessionId")
                if not session_id:
                    continue
                ts_ms = int(entry.get("timestamp") or 0)
                existing = result.get(session_id)
                if existing is None or ts_ms >= existing["timestamp_ms"]:
                    result[session_id] = {
                        "timestamp_ms": ts_ms,
                        "project": entry.get("project"),
                        "display": entry.get("display"),
                    }

        self._history_cache = (signature, result)
        return result

    def _session_signature(self, path: Path) -> tuple[int, int]:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _bucket_dict(self) -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "read_tokens": 0,
            "write_tokens": 0,
            "total_tokens": 0,
            "assistant_messages": 0,
            "user_prompts": 0,
            "tool_uses": 0,
            "tool_results": 0,
            "errors": 0,
            "web_search_requests": 0,
        }

    def _telemetry_signature(self) -> tuple[tuple[str, int, int], ...]:
        if not self.telemetry_dir.exists():
            return ()
        signature: list[tuple[str, int, int]] = []
        for path in sorted(self.telemetry_dir.glob("*.json")):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def _scan_telemetry_accounts(self) -> dict[str, dict[str, Any]]:
        signature = self._telemetry_signature()
        if signature == self._telemetry_cache[0]:
            return self._telemetry_cache[1]

        session_accounts: dict[str, dict[str, Any]] = {}
        if self.telemetry_dir.exists():
            for path in sorted(self.telemetry_dir.glob("*.json")):
                for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event = payload.get("event_data") or {}
                    session_id = str(event.get("session_id") or "").strip()
                    auth = event.get("auth") or {}
                    account_uuid = str(auth.get("account_uuid") or "").strip()
                    organization_uuid = str(auth.get("organization_uuid") or "").strip()
                    email = str(payload.get("email") or event.get("email") or "").strip()
                    if not session_id or not any([account_uuid, organization_uuid, email]):
                        continue
                    telemetry_ts = parse_iso_ts(event.get("client_timestamp")) or 0.0
                    account_key = account_uuid or email or f"org:{organization_uuid}"
                    label = email or (f"acct {account_uuid[:8]}" if account_uuid else f"org {organization_uuid[:8]}")
                    current = session_accounts.get(session_id)
                    if current and current.get("telemetry_ts", 0) > telemetry_ts:
                        continue
                    session_accounts[session_id] = {
                        "account_key": account_key,
                        "account_uuid": account_uuid or None,
                        "organization_uuid": organization_uuid or None,
                        "email": email or None,
                        "label": label,
                        "telemetry_ts": telemetry_ts,
                    }

        self._telemetry_cache = (signature, session_accounts)
        return session_accounts

    def _make_bucket_row(
        self,
        bucket_dt: datetime,
        mode: str,
        session_id: str,
        project_path: str | None,
        project_key: str,
        values: dict[str, int],
    ) -> dict[str, Any]:
        return {
            "bucket": bucket_dt.isoformat(),
            "bucket_start": bucket_dt.timestamp(),
            "label": bucket_dt.strftime("%Y-%m-%d %H:00" if mode == "hour" else "%Y-%m-%d"),
            "mode": mode,
            "session_id": session_id,
            "project_key": project_key,
            "project_path": project_path or project_key,
            **values,
        }

    def _parse_session_file(self, path: Path) -> SessionParseResult:
        session_id = path.stem
        project_key = path.parent.name
        session_file = str(path)
        file_mtime = path.stat().st_mtime

        day_buckets: dict[datetime, dict[str, int]] = defaultdict(self._bucket_dict)
        hour_buckets: dict[datetime, dict[str, int]] = defaultdict(self._bucket_dict)
        totals = self._bucket_dict()
        message_counts = Counter()
        model_totals: dict[str, dict[str, int]] = defaultdict(lambda: self._bucket_dict())
        states_tail: list[dict[str, Any]] = []
        observed_projects: list[str] = []
        observed_cwds: list[str] = []
        last_prompt: str | None = None
        first_ts: float | None = None
        last_ts: float | None = None
        last_error_kind: str | None = None
        last_error_text: str | None = None
        total_turn_duration_ms = 0
        parse_errors = 0
        usage_events: list[dict[str, Any]] = []
        limit_events: list[dict[str, Any]] = []

        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            timestamp = parse_iso_ts(entry.get("timestamp"))
            if timestamp is not None:
                first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
                last_ts = timestamp if last_ts is None else max(last_ts, timestamp)

            if entry.get("project"):
                observed_projects.append(str(entry["project"]))
            if entry.get("cwd"):
                observed_cwds.append(str(entry["cwd"]))

            kind = str(entry.get("type") or "")
            if kind:
                message_counts[kind] += 1

            if kind == "last-prompt":
                prompt = entry.get("lastPrompt")
                if prompt:
                    last_prompt = str(prompt).strip()
                continue

            if kind == "system" and entry.get("subtype") == "turn_duration":
                total_turn_duration_ms += int(entry.get("durationMs") or 0)

            message = entry.get("message") or {}
            content = message.get("content")
            stop_reason = message.get("stop_reason")
            has_error = bool(entry.get("error"))
            error_text = extract_error_text(entry) if has_error else None

            if has_error:
                message_counts["error"] += 1
                last_error_kind = str(entry.get("error"))
                last_error_text = error_text or last_error_kind

            if kind == "assistant":
                usage = numeric_usage_totals(message.get("usage") or {})
                merge_counter(totals, usage)
                model = str(message.get("model") or "unknown")
                merge_counter(model_totals[model], usage)

                tool_uses = 0
                if isinstance(content, list):
                    tool_uses = sum(1 for item in content if isinstance(item, dict) and item.get("type") == "tool_use")
                totals["assistant_messages"] += 1
                totals["tool_uses"] += tool_uses
                model_totals[model]["assistant_messages"] += 1
                model_totals[model]["tool_uses"] += tool_uses
                message_counts["assistant_messages"] += 1
                if timestamp is not None:
                    usage_events.append(
                        {
                            "timestamp": timestamp,
                            "model": model,
                            "usage": dict(usage),
                            "tool_uses": tool_uses,
                            "has_error": bool(has_error),
                        }
                    )
                    hour = ts_to_local(timestamp).replace(minute=0, second=0, microsecond=0)
                    day = hour.replace(hour=0)
                    merge_counter(hour_buckets[hour], usage)
                    merge_counter(day_buckets[day], usage)
                    hour_buckets[hour]["assistant_messages"] += 1
                    day_buckets[day]["assistant_messages"] += 1
                    hour_buckets[hour]["tool_uses"] += tool_uses
                    day_buckets[day]["tool_uses"] += tool_uses
                    hour_buckets[hour]["errors"] += int(has_error)
                    day_buckets[day]["errors"] += int(has_error)
                    hour_buckets[hour]["web_search_requests"] += usage["web_search_requests"]
                    day_buckets[day]["web_search_requests"] += usage["web_search_requests"]
                if timestamp is not None and str(entry.get("error") or "").strip() == "rate_limit":
                    limit_events.append(
                        {
                            "timestamp": timestamp,
                            "kind": "rate_limit",
                            "label": error_text or "You've hit your limit",
                            "error": "rate_limit",
                            "model": model,
                        }
                    )

            if kind == "user":
                text = extract_user_text(content)
                if text:
                    last_prompt = text
                tool_result_count = 0
                tool_result_errors = 0
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_result_count += 1
                            if item.get("is_error"):
                                tool_result_errors += 1
                is_prompt = bool(text) and not is_tool_result_only(content)
                totals["tool_results"] += tool_result_count
                message_counts["tool_results"] += tool_result_count
                message_counts["user_messages"] += 1
                if is_prompt:
                    totals["user_prompts"] += 1
                    message_counts["user_prompts"] += 1
                if tool_result_errors:
                    totals["errors"] += tool_result_errors
                    message_counts["error"] += tool_result_errors
                    last_error_kind = "tool_result_error"
                    last_error_text = extract_error_text(entry) or "Tool result error"
                if timestamp is not None:
                    hour = ts_to_local(timestamp).replace(minute=0, second=0, microsecond=0)
                    day = hour.replace(hour=0)
                    hour_buckets[hour]["tool_results"] += tool_result_count
                    day_buckets[day]["tool_results"] += tool_result_count
                    hour_buckets[hour]["errors"] += tool_result_errors
                    day_buckets[day]["errors"] += tool_result_errors
                    if is_prompt:
                        hour_buckets[hour]["user_prompts"] += 1
                        day_buckets[day]["user_prompts"] += 1

            if kind in {"user", "assistant", "system"}:
                states_tail.append(
                    {
                        "type": kind,
                        "timestamp": timestamp,
                        "stop_reason": stop_reason,
                        "error": entry.get("error"),
                        "subtype": entry.get("subtype"),
                    }
                )
                if len(states_tail) > 8:
                    states_tail.pop(0)

        project_path = observed_cwds[-1] if observed_cwds else (observed_projects[-1] if observed_projects else None)
        duration_seconds = int(max(0, (last_ts or 0) - (first_ts or 0))) if first_ts and last_ts else 0
        recent_error = next((item for item in reversed(states_tail) if item.get("error")), None)
        latest_real = next((item for item in reversed(states_tail) if item.get("type") in {"user", "assistant", "system"}), None)

        state = "unknown"
        if recent_error and latest_real and (latest_real.get("timestamp") or 0) - (recent_error.get("timestamp") or 0) <= 5:
            state = "rate_limited" if recent_error.get("error") == "rate_limit" else "error"
        elif latest_real and latest_real.get("type") == "assistant" and latest_real.get("stop_reason") == "tool_use":
            state = "awaiting_tool_result"
        elif latest_real and latest_real.get("type") == "user":
            state = "awaiting_assistant"
        elif latest_real and latest_real.get("type") == "assistant":
            state = "waiting_for_user"
        elif latest_real and latest_real.get("type") == "system":
            state = "waiting_for_user"

        summary = {
            "session_id": session_id,
            "project_key": project_key,
            "project_path": project_path,
            "session_file": session_file,
            "session_dir": str(path.parent),
            "working_path": project_path or str(path.parent),
            "first_event_at": first_ts,
            "last_event_at": last_ts,
            "last_access_at": max(file_mtime, last_ts or 0),
            "last_access_relative": format_relative(max(file_mtime, last_ts or 0)),
            "duration_seconds": duration_seconds,
            "duration_minutes": round(duration_seconds / 60, 1),
            "message_counts": dict(message_counts),
            "tokens": totals,
            "models": dict(model_totals),
            "last_prompt": last_prompt,
            "last_error_kind": last_error_kind,
            "last_error_text": last_error_text,
            "state": state,
            "turn_duration_ms": total_turn_duration_ms,
            "file_mtime": file_mtime,
            "parse_errors": parse_errors,
            "resume_command": resume_command_for(session_id, project_path),
            "open_command": open_command_for(project_path or str(path.parent)),
        }

        day_rows = [
            self._make_bucket_row(bucket_dt, "day", session_id, project_path, project_key, values)
            for bucket_dt, values in sorted(day_buckets.items())
        ]
        hour_rows = [
            self._make_bucket_row(bucket_dt, "hour", session_id, project_path, project_key, values)
            for bucket_dt, values in sorted(hour_buckets.items())
        ]
        return SessionParseResult(
            summary=summary,
            day_rows=day_rows,
            hour_rows=hour_rows,
            usage_events=usage_events,
            limit_events=limit_events,
        )

    def _session_result(self, path: Path) -> SessionParseResult:
        key = str(path)
        signature = self._session_signature(path)
        cached = self._session_cache.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        parsed = self._parse_session_file(path)
        self._session_cache[key] = (signature, parsed)
        return parsed

    def _stats_cache_summary(self) -> dict[str, Any] | None:
        payload = safe_read_json(self.stats_cache_path)
        if not isinstance(payload, dict):
            return None
        return {
            "last_computed_date": payload.get("lastComputedDate"),
            "total_sessions": payload.get("totalSessions"),
            "total_messages": payload.get("totalMessages"),
            "daily_activity": payload.get("dailyActivity") or [],
            "daily_model_tokens": payload.get("dailyModelTokens") or [],
            "model_usage": payload.get("modelUsage") or {},
        }

    def snapshot(self) -> dict[str, Any]:
        history_map = self._scan_history()
        telemetry_accounts = self._scan_telemetry_accounts()
        session_files = sorted(self.projects_dir.glob("*/*.jsonl")) if self.projects_dir.exists() else []
        sessions: list[dict[str, Any]] = []
        activity_days: list[dict[str, Any]] = []
        activity_hours: list[dict[str, Any]] = []
        usage_events: list[dict[str, Any]] = []
        limit_events: list[dict[str, Any]] = []

        for path in session_files:
            result = self._session_result(path)
            summary = dict(result.summary)
            history_entry = history_map.get(summary["session_id"]) or {}
            if not summary.get("project_path") and history_entry.get("project"):
                summary["project_path"] = history_entry.get("project")
                summary["working_path"] = summary["project_path"]
                summary["resume_command"] = resume_command_for(summary["session_id"], summary["project_path"])
                summary["open_command"] = open_command_for(summary["project_path"])
            if history_entry.get("timestamp_ms"):
                history_ts = int(history_entry["timestamp_ms"]) / 1000
                summary["last_access_at"] = max(summary.get("last_access_at") or 0, history_ts)
                summary["last_access_relative"] = format_relative(summary["last_access_at"])
            if not summary.get("last_prompt") and history_entry.get("display"):
                summary["last_prompt"] = str(history_entry["display"]).strip()
            account = dict(telemetry_accounts.get(summary["session_id"]) or {})
            summary["account_key"] = account.get("account_key") or "unknown"
            summary["account_label"] = account.get("label") or "Unknown account"
            summary["account_email"] = account.get("email")
            summary["account_uuid"] = account.get("account_uuid")
            summary["organization_uuid"] = account.get("organization_uuid")
            sessions.append(summary)
            project_name = Path(str(summary.get("project_path") or summary.get("project_key") or "")).name or (
                summary.get("project_key") or "(unknown)"
            )
            for event in result.usage_events:
                usage = dict(event.get("usage") or {})
                usage_events.append(
                    {
                        "provider": "claude",
                        "timestamp": event.get("timestamp"),
                        "session_id": summary["session_id"],
                        "session_label": summary["session_id"][:12],
                        "project_key": summary.get("project_key"),
                        "project_path": summary.get("project_path") or summary.get("project_key"),
                        "project_name": project_name,
                        "account_key": summary["account_key"],
                        "account_label": summary["account_label"],
                        "account_email": summary["account_email"],
                        "model": event.get("model"),
                        "tool_uses": int(event.get("tool_uses") or 0),
                        "has_error": bool(event.get("has_error")),
                        "usage": {
                            **usage,
                            "cached_tokens": int(usage.get("cache_read_input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0),
                        },
                    }
                )
            for event in result.limit_events:
                limit_events.append(
                    {
                        "provider": "claude",
                        "timestamp": event.get("timestamp"),
                        "session_id": summary["session_id"],
                        "session_label": summary["session_id"][:12],
                        "project_key": summary.get("project_key"),
                        "project_path": summary.get("project_path") or summary.get("project_key"),
                        "project_name": project_name,
                        "account_key": summary["account_key"],
                        "account_label": summary["account_label"],
                        "account_email": summary["account_email"],
                        "kind": event.get("kind") or "rate_limit",
                        "label": event.get("label") or "You've hit your limit",
                        "error": event.get("error") or "rate_limit",
                        "model": event.get("model"),
                    }
                )
            for row in result.day_rows:
                activity_days.append(
                    {
                        **row,
                        "account_key": summary["account_key"],
                        "account_label": summary["account_label"],
                        "account_email": summary["account_email"],
                        "account_uuid": summary["account_uuid"],
                    }
                )
            for row in result.hour_rows:
                activity_hours.append(
                    {
                        **row,
                        "account_key": summary["account_key"],
                        "account_label": summary["account_label"],
                        "account_email": summary["account_email"],
                        "account_uuid": summary["account_uuid"],
                    }
                )

        sessions.sort(key=lambda item: (item.get("last_access_at") or 0, item.get("last_event_at") or 0), reverse=True)

        projects_map: dict[str, dict[str, Any]] = {}
        for session in sessions:
            project_path = str(session.get("project_path") or session.get("project_key") or "unknown")
            project_id = f"{session.get('account_key') or 'unknown'}::{project_path}"
            project = projects_map.setdefault(
                project_id,
                {
                    "project_id": project_id,
                    "project_key": session.get("project_key"),
                    "project_path": project_path,
                    "account_key": session.get("account_key") or "unknown",
                    "account_label": session.get("account_label") or "Unknown account",
                    "account_email": session.get("account_email"),
                    "account_uuid": session.get("account_uuid"),
                    "session_ids": [],
                    "session_count": 0,
                    "last_access_at": 0,
                    "first_event_at": None,
                    "last_event_at": None,
                    "tokens": self._bucket_dict(),
                    "state_counts": Counter(),
                },
            )
            project["session_ids"].append(session["session_id"])
            project["session_count"] += 1
            project["last_access_at"] = max(project["last_access_at"], session.get("last_access_at") or 0)
            first_ts = session.get("first_event_at")
            last_ts = session.get("last_event_at")
            if first_ts is not None:
                project["first_event_at"] = first_ts if project["first_event_at"] is None else min(project["first_event_at"], first_ts)
            if last_ts is not None:
                project["last_event_at"] = last_ts if project["last_event_at"] is None else max(project["last_event_at"], last_ts)
            merge_counter(project["tokens"], session.get("tokens") or {})
            project["state_counts"][session.get("state") or "unknown"] += 1

        projects = sorted(projects_map.values(), key=lambda item: item["last_access_at"], reverse=True)
        for project in projects:
            project["last_access_relative"] = format_relative(project["last_access_at"])
            project["state_counts"] = dict(project["state_counts"])

        activity_days.sort(key=lambda row: (row["bucket_start"], row["project_path"], row["session_id"]))
        activity_hours.sort(key=lambda row: (row["bucket_start"], row["project_path"], row["session_id"]))

        range_rows_day = list(activity_days)
        latest_access = max((session.get("last_access_at") or 0 for session in sessions), default=0)
        generated_at = datetime.now(tz=LOCAL_TZ).timestamp()
        bounds = {
            "min_day": min((row["label"] for row in activity_days), default=None),
            "max_day": max((row["label"] for row in activity_days), default=None),
        }
        accounts_map: dict[str, dict[str, Any]] = {}
        for session in sessions:
            account_key = session.get("account_key") or "unknown"
            account = accounts_map.setdefault(
                account_key,
                {
                    "account_key": account_key,
                    "label": session.get("account_label") or "Unknown account",
                    "email": session.get("account_email"),
                    "account_uuid": session.get("account_uuid"),
                    "organization_uuid": session.get("organization_uuid"),
                    "session_count": 0,
                    "project_paths": set(),
                    "tokens": self._bucket_dict(),
                    "latest_access_at": 0,
                },
            )
            account["session_count"] += 1
            account["project_paths"].add(session.get("project_path") or session.get("project_key") or "unknown")
            merge_counter(account["tokens"], session.get("tokens") or {})
            account["latest_access_at"] = max(account["latest_access_at"], session.get("last_access_at") or 0)
        accounts = []
        for account in accounts_map.values():
            accounts.append(
                {
                    "account_key": account["account_key"],
                    "label": account["label"],
                    "email": account["email"],
                    "account_uuid": account["account_uuid"],
                    "organization_uuid": account["organization_uuid"],
                    "session_count": account["session_count"],
                    "project_count": len(account["project_paths"]),
                    "tokens": account["tokens"],
                    "latest_access_at": account["latest_access_at"],
                    "latest_access_relative": format_relative(account["latest_access_at"]) if account["latest_access_at"] else "-",
                }
            )
        accounts.sort(key=lambda item: (item["session_count"], item["tokens"]["total_tokens"], item["label"]), reverse=True)

        overview = {
            "session_count": len(sessions),
            "project_count": len(projects),
            "read_tokens_total": sum(int(session.get("tokens", {}).get("read_tokens", 0) or 0) for session in sessions),
            "write_tokens_total": sum(int(session.get("tokens", {}).get("write_tokens", 0) or 0) for session in sessions),
            "tokens_total": sum(int(session.get("tokens", {}).get("total_tokens", 0) or 0) for session in sessions),
            "read_tokens_last_5d": window_total(range_rows_day, 5, field="read_tokens"),
            "write_tokens_last_5d": window_total(range_rows_day, 5, field="write_tokens"),
            "read_tokens_last_7d": window_total(range_rows_day, 7, field="read_tokens"),
            "write_tokens_last_7d": window_total(range_rows_day, 7, field="write_tokens"),
            "latest_access_at": latest_access,
            "latest_access_relative": format_relative(latest_access) if latest_access else "-",
            "rate_limited_sessions": sum(1 for session in sessions if session.get("state") == "rate_limited"),
            "error_sessions": sum(1 for session in sessions if session.get("state") == "error"),
            "waiting_sessions": sum(
                1
                for session in sessions
                if session.get("state") in {"awaiting_assistant", "awaiting_tool_result", "waiting_for_user"}
            ),
        }

        sources = {
            "root_dir": str(self.root_dir),
            "projects_dir": str(self.projects_dir),
            "history_path": str(self.history_path),
            "stats_cache_path": str(self.stats_cache_path),
            "settings_path": str(self.settings_path),
            "telemetry_dir": str(self.telemetry_dir),
        }

        return {
            "generated_at": generated_at,
            "timezone": str(LOCAL_TZ),
            "sources": sources,
            "overview": overview,
            "bounds": bounds,
            "accounts": accounts,
            "sessions": sessions,
            "projects": projects,
            "activity": {
                "day_rows": activity_days,
                "hour_rows": activity_hours,
            },
            "usage_events": usage_events,
            "limit_events": limit_events,
            "stats_cache": self._stats_cache_summary(),
            "settings": safe_read_json(self.settings_path),
        }


def html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Claude Session Dashboard</title>
  <style>
    :root{
      --bg:#08111d;
      --bg2:#0d1726;
      --panel:#101d2f;
      --panel-2:#0a1524;
      --line:rgba(155,188,222,.16);
      --ink:#e9f3ff;
      --muted:#9bb1c8;
      --accent:#37c7a7;
      --accent-2:#62a7ff;
      --accent-soft:rgba(55,199,167,.14);
      --danger:#ff7a7a;
      --danger-soft:rgba(255,122,122,.14);
      --ok:#7be0a9;
      --ok-soft:rgba(123,224,169,.14);
      --shadow:0 18px 50px rgba(0,0,0,.28);
      --radius:20px;
      --radius-sm:12px;
      --sans:ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --mono:ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    *{box-sizing:border-box}
    body{margin:0;background:
      radial-gradient(circle at top left, rgba(55,199,167,.16), transparent 28%),
      radial-gradient(circle at top right, rgba(98,167,255,.14), transparent 24%),
      linear-gradient(180deg, var(--bg) 0%, var(--bg2) 55%, #07101b 100%);
      color:var(--ink);font-family:var(--sans);line-height:1.45}
    button,input,select{font:inherit}
    .nav{position:sticky;top:0;z-index:20;background:rgba(8,17,29,.84);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
    .nav-inner{max-width:1560px;margin:0 auto;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px}
    .brand{font-weight:800;letter-spacing:.02em}
    .brand small{display:block;color:var(--muted);font-weight:600}
    .status{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:14px}
    .dot{width:10px;height:10px;border-radius:999px;background:var(--accent);box-shadow:0 0 0 4px rgba(55,199,167,.12)}
    .shell{max-width:1560px;margin:0 auto;padding:24px}
    .hero{display:grid;grid-template-columns:1.2fr .8fr;gap:20px;align-items:stretch}
    .hero-card,.stats-card,.panel{background:linear-gradient(180deg, rgba(18,31,49,.95), rgba(12,24,40,.95));border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
    .hero-card{padding:24px}
    .hero-card h1{margin:0;font-size:34px;line-height:1.05;letter-spacing:-.03em}
    .hero-card p{margin:10px 0 0;color:var(--muted);max-width:62ch}
    .hero-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
    .stats-card{padding:18px}
    .eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);font-weight:700}
    .big{font-size:28px;font-weight:800;letter-spacing:-.03em;margin-top:6px}
    .sub{color:var(--muted);font-size:13px;margin-top:4px}
    .controls{margin-top:20px;display:flex;flex-wrap:wrap;gap:10px;align-items:end}
    .account-strip{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
    .account-strip-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
    .account-strip-head .eyebrow{margin:0}
    .account-tabs{display:flex;gap:8px;flex-wrap:wrap}
    .account-tab{background:#0a1524;border:1px solid rgba(155,188,222,.18);border-radius:999px;padding:9px 12px;cursor:pointer;min-width:0;color:var(--ink)}
    .account-tab.active{background:var(--ink);border-color:var(--ink);color:#fff}
    .account-tab .label{font-weight:800;display:block;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .account-tab .meta{font-size:11px;color:var(--muted)}
    .account-tab.active .meta{color:rgba(255,255,255,.78)}
    .field{display:flex;flex-direction:column;gap:6px}
    .field label{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
    input,select{background:#0a1524;border:1px solid rgba(155,188,222,.18);border-radius:10px;padding:10px 12px;color:var(--ink)}
    .btn{border:none;border-radius:999px;padding:10px 14px;font-weight:700;cursor:pointer}
    .btn-primary{background:linear-gradient(135deg, rgba(55,199,167,.26), rgba(98,167,255,.24));color:var(--ink);border:1px solid rgba(155,188,222,.18)}
    .btn-soft{background:#0a1524;color:var(--ink);border:1px solid rgba(155,188,222,.18)}
    .btn-quick{background:#0a1524;border:1px solid rgba(155,188,222,.18);color:var(--ink)}
    .btn-quick.active{background:var(--ink);color:#08111d;border-color:var(--ink)}
    .layout{display:grid;grid-template-columns:1.35fr .85fr;gap:20px;margin-top:20px;align-items:stretch}
    .layout > .panel{height:min(76vh, 920px);min-height:620px;display:flex;flex-direction:column;min-width:0}
    .panel-head{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
    .panel-head h2{margin:0;font-size:18px}
    .tabs{display:flex;gap:8px;flex-wrap:wrap}
    .tab{background:transparent;border:1px solid var(--line);border-radius:999px;padding:8px 12px;font-weight:700;cursor:pointer}
    .tab.active{background:var(--ink);border-color:var(--ink);color:#08111d}
    .chart{padding:18px 20px 8px;flex:0 0 auto}
    .bars{height:220px;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(28px,1fr);gap:8px;align-items:end}
    .bar-wrap{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:0}
    .bar{width:100%;border-radius:10px 10px 4px 4px;background:linear-gradient(180deg,var(--accent),#49a39c);min-height:6px;cursor:pointer;transition:transform .15s ease}
    .bar.warn{background:linear-gradient(180deg,var(--accent-2),#7fb6ff)}
    .bar:hover{transform:translateY(-3px)}
    .bar-label,.bar-value{font-size:11px;color:var(--muted);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
    .table-wrap{padding:0 20px 20px;overflow:auto;flex:1 1 auto;min-height:0}
    table{width:100%;border-collapse:collapse;font-size:14px}
    th,td{text-align:left;padding:12px 10px;border-bottom:1px solid rgba(155,188,222,.1);vertical-align:top;overflow-wrap:anywhere;word-break:break-word}
    th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;position:sticky;top:0;background:#102035}
    tr.selectable{cursor:pointer}
    tr.selectable:hover{background:rgba(98,167,255,.08)}
    tr.selectable.active{background:rgba(55,199,167,.10)}
    .mono{font-family:var(--mono)}
    .pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:700}
    .pill.ok{background:var(--ok-soft);color:var(--ok)}
    .pill.warn{background:rgba(98,167,255,.14);color:#cfe3ff}
    .pill.err{background:var(--danger-soft);color:var(--danger)}
    .pill.idle{background:rgba(155,188,222,.12);color:var(--muted)}
    .detail{padding:20px;overflow:auto;flex:1 1 auto;min-height:0}
    .detail h3{margin:0;font-size:24px;line-height:1.1}
    .detail p{color:var(--muted);overflow-wrap:anywhere;word-break:break-word}
    .chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 18px}
    .chip{border-radius:999px;background:#0a1524;padding:7px 10px;font-size:12px;font-weight:700;border:1px solid rgba(155,188,222,.12);overflow-wrap:anywhere}
    .kv{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .kv-item{padding:12px;border:1px solid rgba(155,188,222,.12);border-radius:14px;background:rgba(7,16,27,.55)}
    .kv-item .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:700}
    .kv-item .v{margin-top:4px;font-size:14px;font-weight:700;word-break:break-word}
    .section{margin-top:18px}
    .section h4{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
    .mini-list{display:flex;flex-direction:column;gap:10px}
    .mini-item{padding:12px;border:1px solid rgba(155,188,222,.12);border-radius:14px;background:rgba(7,16,27,.55)}
    .mini-item .title{font-weight:700;overflow-wrap:anywhere;word-break:break-word}
    .mini-item .meta{color:var(--muted);font-size:13px;margin-top:4px;overflow-wrap:anywhere;word-break:break-word}
    .empty{padding:28px 20px;color:var(--muted)}
    .footer-note{margin-top:10px;color:var(--muted);font-size:12px}
    @media(max-width:1100px){
      .hero,.layout{grid-template-columns:1fr}
      .layout > .panel{height:auto;min-height:0}
    }
    @media(max-width:720px){
      .shell{padding:14px}
      .hero-grid,.kv{grid-template-columns:1fr}
      .bars{grid-auto-columns:minmax(22px,1fr);height:180px}
    }
  </style>
</head>
<body>
  <nav class="nav">
    <div class="nav-inner">
      <div class="brand">Claude Session Dashboard<small>Local analytics for ~/.claude</small></div>
      <div class="status"><span class="dot"></span><span id="nav-status">Loading…</span></div>
    </div>
  </nav>

  <div class="shell">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">Source Map</div>
        <h1>Projects, sessions, tokens, and state from local Claude Code files</h1>
        <p>This dashboard reads session transcripts from <span class="mono">~/.claude/projects</span>, prompt history from <span class="mono">history.jsonl</span>, and cached rollups from <span class="mono">stats-cache.json</span>. Range filters and day/hour views are computed locally in your browser.</p>

        <div class="controls">
          <div class="field">
            <label for="range-start">Start</label>
            <input id="range-start" type="date">
          </div>
          <div class="field">
            <label for="range-end">End</label>
            <input id="range-end" type="date">
          </div>
          <div class="field">
            <label for="bucket-mode">Bucket</label>
            <select id="bucket-mode">
              <option value="day">Day</option>
              <option value="hour">Hour</option>
            </select>
          </div>
          <div class="field" style="min-width:220px">
            <label for="search">Search</label>
            <input id="search" type="search" placeholder="Project path, session id, prompt text">
          </div>
          <button class="btn btn-quick" data-range="5">5D</button>
          <button class="btn btn-quick" data-range="7">7D</button>
          <button class="btn btn-quick" data-range="30">30D</button>
          <button class="btn btn-quick" data-range="all">All</button>
          <button class="btn btn-primary" id="refresh">Refresh</button>
        </div>

        <div class="account-strip">
          <div class="account-strip-head">
            <div class="eyebrow">Account Scope</div>
            <div class="sub" id="account-scope-note">All local Claude accounts</div>
          </div>
          <div class="account-tabs" id="account-tabs"></div>
        </div>
      </div>

      <div class="hero-grid">
        <div class="stats-card"><div class="eyebrow">Sessions</div><div class="big" id="metric-sessions">-</div><div class="sub" id="metric-sessions-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow">Projects</div><div class="big" id="metric-projects">-</div><div class="sub" id="metric-projects-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow" id="metric-read-label">Read Range</div><div class="big" id="metric-read-5d">-</div><div class="sub" id="metric-read-5d-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow" id="metric-write-label">Write Range</div><div class="big" id="metric-write-5d">-</div><div class="sub" id="metric-write-5d-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow" id="metric-range-read-label">Selected Read</div><div class="big" id="metric-range-read">-</div><div class="sub" id="metric-range-read-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow" id="metric-range-write-label">Selected Write</div><div class="big" id="metric-range-write">-</div><div class="sub" id="metric-range-write-sub">-</div></div>
        <div class="stats-card"><div class="eyebrow">Last Access</div><div class="big" id="metric-last">-</div><div class="sub" id="metric-last-sub">-</div></div>
      </div>
    </section>

    <section class="layout">
      <div class="panel">
        <div class="panel-head">
          <h2>Explorer</h2>
          <div class="tabs">
            <button class="tab active" data-view="sessions">Sessions</button>
            <button class="tab" data-view="projects">Projects</button>
            <button class="tab" data-view="activity">Activity</button>
          </div>
        </div>
        <div class="chart">
          <div class="eyebrow" id="chart-title">Tokens by Day</div>
          <div class="bars" id="bars"></div>
          <div class="footer-note" id="chart-note">Select a bucket to inspect the contributing sessions and projects.</div>
        </div>
        <div class="table-wrap" id="table-wrap"></div>
      </div>

      <div class="panel">
        <div class="detail" id="detail"></div>
      </div>
    </section>
  </div>

  <script>
    const state = {
      snapshot: null,
      view: 'sessions',
      selected: null,
      accountKey: 'all',
    };

    function el(id){ return document.getElementById(id); }
    function fmt(n){ return new Intl.NumberFormat().format(Number(n || 0)); }
    function compact(n) {
      const value = Number(n || 0);
      const abs = Math.abs(value);
      if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(abs >= 10_000_000_000 ? 0 : 1)}B`;
      if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
      return fmt(value);
    }
    function formatLocalDateInput(value) {
      const year = value.getFullYear();
      const month = String(value.getMonth() + 1).padStart(2, '0');
      const day = String(value.getDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    }
    function fmtDate(ts){ return ts ? new Date(ts * 1000).toLocaleString() : '-'; }
    function fmtShort(ts){ return ts ? new Date(ts * 1000).toLocaleDateString() : '-'; }

    function pillClass(kind) {
      if (kind === 'rate_limited' || kind === 'error') return 'err';
      if (kind === 'awaiting_assistant' || kind === 'awaiting_tool_result') return 'warn';
      if (kind === 'waiting_for_user') return 'ok';
      return 'idle';
    }

    function api(path) {
      return fetch(path).then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
      });
    }

    function currentRange() {
      const start = el('range-start').value ? new Date(`${el('range-start').value}T00:00:00`).getTime() / 1000 : null;
      const end = el('range-end').value ? new Date(`${el('range-end').value}T23:59:59`).getTime() / 1000 : null;
      return { start, end };
    }

    function activeRangeToken() {
      return document.querySelector('[data-range].active')?.dataset.range || null;
    }

    function currentRangeLabel() {
      const active = activeRangeToken();
      if (active === 'all') return 'All';
      if (active) return `${active}D`;
      const start = el('range-start').value;
      const end = el('range-end').value;
      if (start && end) {
        if (start === end) return start;
        return `${start} to ${end}`;
      }
      return 'Range';
    }

    function clearQuickRangeSelection() {
      document.querySelectorAll('[data-range]').forEach((btn) => btn.classList.remove('active'));
    }

    function inRange(ts, start, end) {
      if (!ts) return false;
      if (start != null && ts < start) return false;
      if (end != null && ts > end) return false;
      return true;
    }

    function activeAccountKey() {
      return state.accountKey || 'all';
    }

    function rowMatchesAccount(row) {
      return activeAccountKey() === 'all' || (row.account_key || 'unknown') === activeAccountKey();
    }

    function selectedAccount() {
      if (activeAccountKey() === 'all') return null;
      return (state.snapshot?.accounts || []).find((account) => account.account_key === activeAccountKey()) || null;
    }

    function filteredRows() {
      const mode = el('bucket-mode').value;
      const rows = mode === 'hour' ? (state.snapshot?.activity?.hour_rows || []) : (state.snapshot?.activity?.day_rows || []);
      const { start, end } = currentRange();
      return rows.filter((row) => rowMatchesAccount(row) && inRange(row.bucket_start, start, end));
    }

    function accountScopedRows() {
      const mode = el('bucket-mode').value;
      const rows = mode === 'hour' ? (state.snapshot?.activity?.hour_rows || []) : (state.snapshot?.activity?.day_rows || []);
      return rows.filter((row) => rowMatchesAccount(row));
    }

    function sessionRangeMap() {
      const map = {};
      filteredRows().forEach((row) => {
        const current = map[row.session_id] || {
          total_tokens: 0, read_tokens: 0, write_tokens: 0, assistant_messages: 0, user_prompts: 0, tool_uses: 0, tool_results: 0, errors: 0
        };
        current.total_tokens += row.total_tokens || 0;
        current.read_tokens += row.read_tokens || 0;
        current.write_tokens += row.write_tokens || 0;
        current.assistant_messages += row.assistant_messages || 0;
        current.user_prompts += row.user_prompts || 0;
        current.tool_uses += row.tool_uses || 0;
        current.tool_results += row.tool_results || 0;
        current.errors += row.errors || 0;
        map[row.session_id] = current;
      });
      return map;
    }

    function projectRangeMap() {
      const map = {};
      filteredRows().forEach((row) => {
        const key = `${row.account_key || 'unknown'}::${row.project_path || row.project_key}`;
        const current = map[key] || { total_tokens: 0, read_tokens: 0, write_tokens: 0, assistant_messages: 0, user_prompts: 0, sessions: new Set() };
        current.total_tokens += row.total_tokens || 0;
        current.read_tokens += row.read_tokens || 0;
        current.write_tokens += row.write_tokens || 0;
        current.assistant_messages += row.assistant_messages || 0;
        current.user_prompts += row.user_prompts || 0;
        current.sessions.add(row.session_id);
        map[key] = current;
      });
      return map;
    }

    function filteredSessions() {
      const search = (el('search').value || '').toLowerCase().trim();
      const rangeMap = sessionRangeMap();
      return (state.snapshot?.sessions || []).filter((session) => {
        if (!rowMatchesAccount(session)) return false;
        const hay = `${session.session_id} ${session.project_path || ''} ${session.last_prompt || ''}`.toLowerCase();
        if (search && !hay.includes(search)) return false;
        return true;
      }).map((session) => ({
        ...session,
        range: rangeMap[session.session_id] || {
          total_tokens: 0, read_tokens: 0, write_tokens: 0, assistant_messages: 0, user_prompts: 0, tool_uses: 0, tool_results: 0, errors: 0
        }
      }))
        .sort((a, b) => (b.range.total_tokens - a.range.total_tokens) || ((b.last_access_at || 0) - (a.last_access_at || 0)));
    }

    function filteredProjects() {
      const search = (el('search').value || '').toLowerCase().trim();
      const rangeMap = projectRangeMap();
      return (state.snapshot?.projects || []).filter((project) => {
        if (!rowMatchesAccount(project)) return false;
        const hay = `${project.project_path || ''} ${project.project_key || ''}`.toLowerCase();
        return !search || hay.includes(search);
      }).map((project) => {
        const range = rangeMap[project.project_id] || { total_tokens: 0, read_tokens: 0, write_tokens: 0, assistant_messages: 0, user_prompts: 0, sessions: new Set() };
        return {
          ...project,
          range_tokens: range.total_tokens,
          range_read_tokens: range.read_tokens,
          range_write_tokens: range.write_tokens,
          range_assistant_messages: range.assistant_messages,
          range_user_prompts: range.user_prompts,
          range_session_count: range.sessions.size,
        };
      }).sort((a, b) => (b.range_tokens - a.range_tokens) || ((b.last_access_at || 0) - (a.last_access_at || 0)));
    }

    function groupedActivity() {
      const rows = filteredRows();
      const map = {};
      rows.forEach((row) => {
        const key = row.bucket;
        const current = map[key] || {
          bucket: row.bucket,
          bucket_start: row.bucket_start,
          label: row.label,
          total_tokens: 0,
          read_tokens: 0,
          write_tokens: 0,
          assistant_messages: 0,
          user_prompts: 0,
          sessions: new Set(),
          projects: new Map(),
          breakdown: [],
        };
        current.total_tokens += row.total_tokens || 0;
        current.read_tokens += row.read_tokens || 0;
        current.write_tokens += row.write_tokens || 0;
        current.assistant_messages += row.assistant_messages || 0;
        current.user_prompts += row.user_prompts || 0;
        current.sessions.add(row.session_id);
        current.projects.set(`${row.account_label || 'Unknown'} / ${row.project_path}`, (current.projects.get(`${row.account_label || 'Unknown'} / ${row.project_path}`) || 0) + (row.total_tokens || 0));
        current.breakdown.push(row);
        map[key] = current;
      });
      return Object.values(map).sort((a, b) => a.bucket_start - b.bucket_start);
    }

    function ensureSelected(items, prefix) {
      if (!items.length) {
        state.selected = null;
        return;
      }
      const found = items.find((item) => `${prefix}:${item.session_id || item.project_id || item.bucket}` === state.selected);
      if (!found) {
        const first = items[0];
        state.selected = `${prefix}:${first.session_id || first.project_id || first.bucket}`;
      }
    }

    function renderMetrics() {
      const rows = filteredRows();
      const sessions = filteredSessions();
      const projects = filteredProjects();
      const account = selectedAccount();
      const rangeLabel = currentRangeLabel();
      const rangeRead = rows.reduce((sum, row) => sum + (row.read_tokens || 0), 0);
      const rangeWrite = rows.reduce((sum, row) => sum + (row.write_tokens || 0), 0);
      const rangePrompts = rows.reduce((sum, row) => sum + (row.user_prompts || 0), 0);
      const rangeSessions = new Set(rows.map((row) => row.session_id));
      const waitingCount = sessions.filter((session) => ['awaiting_assistant', 'awaiting_tool_result', 'waiting_for_user'].includes(session.state)).length;
      const limitedCount = sessions.filter((session) => session.state === 'rate_limited').length;
      const errorCount = sessions.filter((session) => session.state === 'error').length;
      const lastAccessAt = Math.max(...sessions.map((session) => session.last_access_at || 0), 0);
      el('metric-sessions').textContent = compact(sessions.length);
      el('metric-sessions-sub').textContent = `${compact(waitingCount)} waiting, ${compact(limitedCount)} limited`;
      el('metric-projects').textContent = compact(projects.length);
      el('metric-projects-sub').textContent = `${compact(errorCount)} error-like sessions`;
      el('metric-read-label').textContent = `Read ${rangeLabel}`;
      el('metric-write-label').textContent = `Write ${rangeLabel}`;
      el('metric-range-read-label').textContent = `Selected Read`;
      el('metric-range-write-label').textContent = `Selected Write`;
      el('metric-read-5d').textContent = compact(rangeRead);
      el('metric-read-5d-sub').textContent = `${compact(rangeRead + rangeWrite)} total tokens in ${rangeLabel.toLowerCase()}`;
      el('metric-write-5d').textContent = compact(rangeWrite);
      el('metric-write-5d-sub').textContent = `${compact(rangeSessions.size)} sessions in ${rangeLabel.toLowerCase()}`;
      el('metric-range-read').textContent = compact(rangeRead);
      el('metric-range-read-sub').textContent = `${rangeLabel} • ${compact(rangePrompts)} prompts across ${compact(rangeSessions.size)} sessions`;
      el('metric-range-write').textContent = compact(rangeWrite);
      el('metric-range-write-sub').textContent = `${rangeLabel} • ${compact(rangeRead + rangeWrite)} total tokens`;
      el('metric-last').textContent = lastAccessAt ? new Date(lastAccessAt * 1000).toLocaleString(undefined, { hour: 'numeric', minute: '2-digit' }) : '-';
      el('metric-last-sub').textContent = lastAccessAt ? fmtDate(lastAccessAt) : '-';
      el('account-scope-note').textContent = account
        ? `${account.label} • ${compact(account.session_count)} sessions • ${compact(account.tokens?.read_tokens || 0)} read / ${compact(account.tokens?.write_tokens || 0)} write`
        : `All local Claude accounts • ${compact(state.snapshot?.accounts?.length || 0)} detected`;
      el('nav-status').textContent = state.snapshot ? `Updated ${new Date(state.snapshot.generated_at * 1000).toLocaleTimeString()}` : 'Loading…';
    }

    function renderBars() {
      const mode = el('bucket-mode').value;
      const groups = groupedActivity();
      el('chart-title').textContent = `Tokens by ${mode === 'hour' ? 'Hour' : 'Day'}`;
      const bars = el('bars');
      bars.innerHTML = '';
      if (!groups.length) {
        bars.innerHTML = '<div class="empty">No activity in the selected range.</div>';
        return;
      }
      const max = Math.max(...groups.map((group) => group.total_tokens || 0), 1);
      groups.forEach((group) => {
        const wrap = document.createElement('div');
        wrap.className = 'bar-wrap';
        const bar = document.createElement('div');
        bar.className = 'bar' + (group.total_tokens >= max * 0.8 ? ' warn' : '');
        bar.style.height = `${Math.max(8, Math.round((group.total_tokens / max) * 180))}px`;
        bar.title = `${group.label}: ${compact(group.read_tokens)} read / ${compact(group.write_tokens)} write`;
        bar.addEventListener('click', () => {
          state.view = 'activity';
          document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.view === 'activity'));
          state.selected = `activity:${group.bucket}`;
          render();
        });
        wrap.appendChild(document.createElement('div')).className = 'bar-value';
        wrap.firstChild.textContent = compact(group.total_tokens);
        wrap.appendChild(bar);
        wrap.appendChild(document.createElement('div')).className = 'bar-label';
        wrap.lastChild.textContent = group.label;
        bars.appendChild(wrap);
      });
    }

    function renderAccountTabs() {
      const wrap = el('account-tabs');
      const accounts = state.snapshot?.accounts || [];
      const items = [{
        account_key: 'all',
        label: 'All Accounts',
        session_count: (state.snapshot?.sessions || []).length,
        project_count: (state.snapshot?.projects || []).length,
      }, ...accounts];
      wrap.innerHTML = items.map((account) => `
        <button class="account-tab ${activeAccountKey() === account.account_key ? 'active' : ''}" data-account-key="${account.account_key}">
          <span class="label">${account.label}</span>
          <span class="meta">${compact(account.session_count || 0)} sessions • ${compact(account.project_count || 0)} projects</span>
        </button>
      `).join('');
      wrap.querySelectorAll('[data-account-key]').forEach((tab) => {
        tab.addEventListener('click', () => {
          state.accountKey = tab.dataset.accountKey;
          state.selected = null;
          render();
        });
      });
    }

    function renderSessionsTable() {
      const items = filteredSessions();
      ensureSelected(items, 'session');
      const active = state.selected?.split(':')[1];
      if (!items.length) {
        el('table-wrap').innerHTML = '<div class="empty">No sessions matched the current filters.</div>';
        return;
      }
      let html = '<table><thead><tr><th>State</th><th>Account</th><th>Session</th><th>Project</th><th>Last Access</th><th>Range Read</th><th>Range Write</th><th>Total</th><th>Prompt</th></tr></thead><tbody>';
      items.forEach((item) => {
        html += `<tr class="selectable ${item.session_id === active ? 'active' : ''}" data-select="session:${item.session_id}">
          <td><span class="pill ${pillClass(item.state)}">${item.state}</span></td>
          <td>${item.account_label || 'Unknown'}</td>
          <td class="mono">${item.session_id.slice(0, 8)}</td>
          <td>${item.project_path || item.project_key || '-'}</td>
          <td>${item.last_access_relative}</td>
          <td class="mono">${compact(item.range.read_tokens)}</td>
          <td class="mono">${compact(item.range.write_tokens)}</td>
          <td class="mono">${compact(item.tokens?.total_tokens)}</td>
          <td>${(item.last_prompt || '-').slice(0, 110)}</td>
        </tr>`;
      });
      html += '</tbody></table>';
      el('table-wrap').innerHTML = html;
    }

    function renderProjectsTable() {
      const items = filteredProjects();
      ensureSelected(items, 'project');
      const active = state.selected?.split(':').slice(1).join(':');
      if (!items.length) {
        el('table-wrap').innerHTML = '<div class="empty">No projects matched the current filters.</div>';
        return;
      }
      let html = '<table><thead><tr><th>Account</th><th>Project</th><th>Sessions</th><th>Last Access</th><th>Range Read</th><th>Range Write</th><th>Total</th><th>States</th></tr></thead><tbody>';
      items.forEach((item) => {
        const states = Object.entries(item.state_counts || {}).map(([key, val]) => `${key}:${val}`).join(' ');
        html += `<tr class="selectable ${item.project_id === active ? 'active' : ''}" data-select="project:${item.project_id}">
          <td>${item.account_label || 'Unknown'}</td>
          <td>${item.project_path}</td>
          <td class="mono">${fmt(item.session_count)}</td>
          <td>${item.last_access_relative}</td>
          <td class="mono">${compact(item.range_read_tokens)}</td>
          <td class="mono">${compact(item.range_write_tokens)}</td>
          <td class="mono">${compact(item.tokens?.total_tokens)}</td>
          <td>${states || '-'}</td>
        </tr>`;
      });
      html += '</tbody></table>';
      el('table-wrap').innerHTML = html;
    }

    function renderActivityTable() {
      const items = groupedActivity();
      ensureSelected(items, 'activity');
      const active = state.selected?.split(':').slice(1).join(':');
      if (!items.length) {
        el('table-wrap').innerHTML = '<div class="empty">No activity buckets matched the current filters.</div>';
        return;
      }
      let html = '<table><thead><tr><th>Bucket</th><th>Read</th><th>Write</th><th>Prompts</th><th>Assistant Msgs</th><th>Sessions</th><th>Top Project</th></tr></thead><tbody>';
      items.forEach((item) => {
        const topProject = [...item.projects.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '-';
        html += `<tr class="selectable ${item.bucket === active ? 'active' : ''}" data-select="activity:${item.bucket}">
          <td>${item.label}</td>
          <td class="mono">${compact(item.read_tokens)}</td>
          <td class="mono">${compact(item.write_tokens)}</td>
          <td class="mono">${compact(item.user_prompts)}</td>
          <td class="mono">${compact(item.assistant_messages)}</td>
          <td class="mono">${compact(item.sessions.size)}</td>
          <td>${topProject}</td>
        </tr>`;
      });
      html += '</tbody></table>';
      el('table-wrap').innerHTML = html;
    }

    function detailSession() {
      const sessionId = state.selected?.split(':')[1];
      const session = (state.snapshot?.sessions || []).find((item) => item.session_id === sessionId);
      if (!session) return '<div class="empty">Select a session to inspect it.</div>';
      const models = Object.entries(session.models || {}).sort((a, b) => (b[1].total_tokens || 0) - (a[1].total_tokens || 0));
      const range = sessionRangeMap()[session.session_id] || { total_tokens: 0, assistant_messages: 0, user_prompts: 0, tool_uses: 0, tool_results: 0, errors: 0 };
      return `
        <div class="eyebrow">Session Detail</div>
        <h3 class="mono">${session.session_id}</h3>
        <p>${session.project_path || session.project_key || '-'}</p>
        <div class="chips">
          <span class="chip">${session.account_label || 'Unknown account'}</span>
          <span class="chip">${session.state}</span>
          <span class="chip">last access ${session.last_access_relative}</span>
          <span class="chip">range ${compact(range.read_tokens)} read / ${compact(range.write_tokens)} write</span>
          <span class="chip">total ${compact(session.tokens?.total_tokens)} tokens</span>
        </div>
        <div class="kv">
          <div class="kv-item"><div class="k">First Event</div><div class="v">${fmtDate(session.first_event_at)}</div></div>
          <div class="kv-item"><div class="k">Last Event</div><div class="v">${fmtDate(session.last_event_at)}</div></div>
          <div class="kv-item"><div class="k">Duration</div><div class="v">${fmt(session.duration_minutes)} min</div></div>
          <div class="kv-item"><div class="k">Prompt Count</div><div class="v">${fmt(session.message_counts?.user_prompts || 0)}</div></div>
          <div class="kv-item"><div class="k">Assistant Messages</div><div class="v">${fmt(session.message_counts?.assistant_messages || 0)}</div></div>
          <div class="kv-item"><div class="k">Errors</div><div class="v">${fmt(session.message_counts?.error || 0)}</div></div>
          <div class="kv-item"><div class="k">Read Tokens</div><div class="v">${compact(session.tokens?.read_tokens)}</div></div>
          <div class="kv-item"><div class="k">Write Tokens</div><div class="v">${compact(session.tokens?.write_tokens)}</div></div>
          <div class="kv-item"><div class="k">Input</div><div class="v">${compact(session.tokens?.input_tokens)}</div></div>
          <div class="kv-item"><div class="k">Cache Read + Create</div><div class="v">${compact((session.tokens?.cache_read_input_tokens || 0) + (session.tokens?.cache_creation_input_tokens || 0))}</div></div>
        </div>
        <div class="section">
          <h4>Last Prompt</h4>
          <div class="mini-item">${session.last_prompt || '-'}</div>
        </div>
        <div class="section">
          <h4>Model Totals</h4>
          <div class="mini-list">
            ${models.length ? models.map(([model, totals]) => `<div class="mini-item"><div class="title mono">${model}</div><div class="meta">${compact(totals.read_tokens)} read | ${compact(totals.write_tokens)} write | ${compact(totals.total_tokens)} total</div></div>`).join('') : '<div class="mini-item">No model usage recorded.</div>'}
          </div>
        </div>
        <div class="section">
          <h4>Files</h4>
          <div class="mini-list">
            <div class="mini-item"><div class="title">Session File</div><div class="meta mono">${session.session_file}</div></div>
            <div class="mini-item"><div class="title">Project Folder</div><div class="meta mono">${session.session_dir}</div></div>
          </div>
        </div>
      `;
    }

    function detailProject() {
      const key = state.selected?.split(':').slice(1).join(':');
      const project = (state.snapshot?.projects || []).find((item) => item.project_id === key);
      if (!project) return '<div class="empty">Select a project to inspect it.</div>';
      const sessions = filteredSessions().filter((item) => (item.project_path || item.project_key) === project.project_path && item.account_key === project.account_key).slice(0, 12);
      return `
        <div class="eyebrow">Project Detail</div>
        <h3>${project.project_path}</h3>
        <p>${project.account_label || 'Unknown account'} • ${project.session_count} sessions • last access ${project.last_access_relative}</p>
        <div class="chips">
          <span class="chip">range ${compact(project.range_read_tokens || 0)} read / ${compact(project.range_write_tokens || 0)} write</span>
          <span class="chip">total ${compact(project.tokens?.total_tokens)}</span>
          <span class="chip">${JSON.stringify(project.state_counts || {})}</span>
        </div>
        <div class="kv">
          <div class="kv-item"><div class="k">First Event</div><div class="v">${fmtDate(project.first_event_at)}</div></div>
          <div class="kv-item"><div class="k">Last Event</div><div class="v">${fmtDate(project.last_event_at)}</div></div>
          <div class="kv-item"><div class="k">Read Tokens</div><div class="v">${compact(project.tokens?.read_tokens)}</div></div>
          <div class="kv-item"><div class="k">Write Tokens</div><div class="v">${compact(project.tokens?.write_tokens)}</div></div>
        </div>
        <div class="section">
          <h4>Top Sessions In Range</h4>
          <div class="mini-list">
            ${sessions.length ? sessions.map((session) => `<div class="mini-item"><div class="title mono">${session.session_id}</div><div class="meta">${compact(session.range.read_tokens)} read | ${compact(session.range.write_tokens)} write | ${session.state} | ${session.last_access_relative}</div></div>`).join('') : '<div class="mini-item">No matching sessions in this range.</div>'}
          </div>
        </div>
      `;
    }

    function detailActivity() {
      const key = state.selected?.split(':').slice(1).join(':');
      const bucket = groupedActivity().find((item) => item.bucket === key);
      if (!bucket) return '<div class="empty">Select a bucket to inspect it.</div>';
      const topRows = [...bucket.breakdown].sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0)).slice(0, 14);
      return `
        <div class="eyebrow">Bucket Detail</div>
        <h3>${bucket.label}</h3>
        <p>${compact(bucket.read_tokens)} read and ${compact(bucket.write_tokens)} write across ${compact(bucket.sessions.size)} sessions</p>
        <div class="chips">
          <span class="chip">${compact(bucket.user_prompts)} prompts</span>
          <span class="chip">${compact(bucket.assistant_messages)} assistant messages</span>
          <span class="chip">${compact(bucket.breakdown.length)} session-project rows</span>
        </div>
        <div class="section">
          <h4>Breakdown</h4>
          <div class="mini-list">
            ${topRows.map((row) => `<div class="mini-item"><div class="title">${row.account_label || 'Unknown'} / ${row.project_path}</div><div class="meta mono">${row.session_id.slice(0, 8)} | ${compact(row.read_tokens)} read | ${compact(row.write_tokens)} write | prompts ${compact(row.user_prompts)} | assistant ${compact(row.assistant_messages)}</div></div>`).join('')}
          </div>
        </div>
      `;
    }

    function renderDetail() {
      if (state.view === 'projects') {
        el('detail').innerHTML = detailProject();
      } else if (state.view === 'activity') {
        el('detail').innerHTML = detailActivity();
      } else {
        el('detail').innerHTML = detailSession();
      }
    }

    function renderTable() {
      if (state.view === 'projects') renderProjectsTable();
      else if (state.view === 'activity') renderActivityTable();
      else renderSessionsTable();

      el('table-wrap').querySelectorAll('[data-select]').forEach((row) => {
        row.addEventListener('click', () => {
          state.selected = row.dataset.select;
          render();
        });
      });
    }

    function render() {
      renderAccountTabs();
      renderMetrics();
      renderBars();
      renderTable();
      renderDetail();
    }

    function applyQuickRange(kind) {
      const accountRows = accountScopedRows();
      const latestBucket = Math.max(...accountRows.map((row) => row.bucket_start || 0), 0);
      const maxDay = latestBucket ? formatLocalDateInput(new Date(latestBucket * 1000)) : state.snapshot?.bounds?.max_day;
      if (!maxDay) return;
      const end = new Date(`${maxDay}T00:00:00`);
      const start = new Date(end);
      if (kind === 'all') {
        const minBucket = Math.min(...accountRows.map((row) => row.bucket_start || Infinity));
        el('range-start').value = Number.isFinite(minBucket) ? formatLocalDateInput(new Date(minBucket * 1000)) : (state.snapshot?.bounds?.min_day || '');
        el('range-end').value = maxDay;
      } else {
        start.setDate(start.getDate() - (Number(kind) - 1));
        el('range-start').value = formatLocalDateInput(start);
        el('range-end').value = formatLocalDateInput(end);
      }
      document.querySelectorAll('[data-range]').forEach((btn) => btn.classList.toggle('active', btn.dataset.range === String(kind)));
      render();
    }

    async function load() {
      state.snapshot = await api('/api/state');
      const knownKeys = new Set(['all', ...(state.snapshot?.accounts || []).map((account) => account.account_key)]);
      if (!knownKeys.has(state.accountKey)) state.accountKey = 'all';
      if (!el('range-start').value && state.snapshot?.bounds?.min_day) {
        applyQuickRange('7');
      } else {
        render();
      }
    }

    function init() {
      document.querySelectorAll('.tab').forEach((tab) => {
        tab.addEventListener('click', () => {
          state.view = tab.dataset.view;
          document.querySelectorAll('.tab').forEach((node) => node.classList.toggle('active', node === tab));
          state.selected = null;
          render();
        });
      });
      document.querySelectorAll('[data-range]').forEach((btn) => btn.addEventListener('click', () => applyQuickRange(btn.dataset.range)));
      ['range-start', 'range-end'].forEach((id) => el(id).addEventListener('input', () => {
        clearQuickRangeSelection();
        render();
      }));
      ['bucket-mode', 'search'].forEach((id) => el(id).addEventListener('input', () => render()));
      el('refresh').addEventListener('click', () => load().catch((err) => alert(err.message)));
      load().catch((err) => {
        el('detail').innerHTML = `<div class="empty">${err.message}</div>`;
        el('nav-status').textContent = 'Failed';
      });
    }

    init();
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
  <title>Claude Session Dashboard</title>
  <style>
    :root{
      --bg:#071019;--bg2:#0b1522;--panel:#0f1b2c;--line:rgba(156,178,205,.15);
      --ink:#eef5ff;--muted:#96abc3;--accent:#64d2b1;--shadow:0 22px 60px rgba(0,0,0,.28);
      --radius:18px;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
      --project-1:#69d0b0;--project-2:#79a7ff;--project-3:#ffb86a;--project-4:#e587ff;--project-5:#6fe0ff;
      --project-6:#ffd26e;--project-7:#ff8f8f;--project-8:#8cf08e;--project-9:#8db8ff;--project-10:#f6a0c8;
    }
    *{box-sizing:border-box}
    body{margin:0;color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at top left, rgba(100,210,177,.13), transparent 24%),radial-gradient(circle at top right, rgba(119,168,255,.12), transparent 20%),linear-gradient(180deg, var(--bg) 0%, var(--bg2) 56%, #09111b 100%)}
    button,input,select{font:inherit}
    .topbar{position:sticky;top:0;z-index:10;backdrop-filter:blur(14px);background:rgba(7,16,25,.82);border-bottom:1px solid var(--line)}
    .topbar-inner{max-width:1680px;margin:0 auto;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;gap:16px}
    .brand{font-size:15px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}.brand small{display:block;font-size:12px;font-weight:600;letter-spacing:0;color:var(--muted);text-transform:none}
    .status{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px}.status-dot{width:9px;height:9px;border-radius:999px;background:var(--accent);box-shadow:0 0 0 4px rgba(100,210,177,.14)}
    .shell{max-width:1680px;margin:0 auto;padding:20px 22px 30px}.panel{background:linear-gradient(180deg, rgba(18,31,49,.96), rgba(10,19,31,.96));border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
    .control-panel{padding:18px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);font-weight:800}
    .hero-copy h1{margin:8px 0 0;font-size:30px;line-height:1.02;letter-spacing:-.03em}.hero-copy p{margin:10px 0 0;color:var(--muted);max-width:74ch}
    .account-tabs{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.account-tab{border:1px solid rgba(156,178,205,.18);background:#0a1420;color:var(--ink);border-radius:999px;padding:10px 14px;min-width:0;cursor:pointer;text-align:left}.account-tab.active{background:var(--ink);color:#0a1420;border-color:var(--ink)}.account-tab .title{font-weight:800;display:block;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.account-tab .meta{display:block;font-size:11px;color:var(--muted);margin-top:2px}.account-tab.active .meta{color:rgba(10,20,32,.68)}
    .filters{margin-top:16px;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;align-items:end}.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:11px;color:var(--muted);font-weight:800;letter-spacing:.08em;text-transform:uppercase}.field input,.field select{width:100%;padding:10px 12px;color:var(--ink);background:#09131f;border:1px solid rgba(156,178,205,.18);border-radius:10px}
    .range-strip{display:flex;gap:8px;flex-wrap:wrap}.chip-btn,.ghost-btn,.primary-btn,.tiny-btn,.tab-btn{border:1px solid rgba(156,178,205,.18);background:#0a1420;color:var(--ink);border-radius:999px;cursor:pointer;font-weight:800}.chip-btn,.tab-btn{padding:9px 12px}.chip-btn.active,.tab-btn.active{background:var(--ink);color:#08101a;border-color:var(--ink)}.primary-btn{padding:10px 14px;background:linear-gradient(135deg, rgba(100,210,177,.26), rgba(119,168,255,.22))}.ghost-btn{padding:10px 14px}.tiny-btn{padding:6px 10px;font-size:12px}
    .metric-grid{margin-top:18px;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}.metric{padding:14px;border-radius:14px;background:rgba(6,13,22,.44);border:1px solid rgba(156,178,205,.11);min-width:0}.metric .label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:800}.metric .value{margin-top:8px;font-size:24px;font-weight:800;letter-spacing:-.03em}.metric .meta{margin-top:6px;font-size:12px;color:var(--muted)}
    .workspace{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(340px,.9fr);gap:16px;margin-top:16px;align-items:start}.panel-head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}.panel-head h2{margin:2px 0 0;font-size:20px}.view-tabs,.timeline-mode{display:flex;gap:8px;flex-wrap:wrap}.content-meta{padding:12px 18px 0;color:var(--muted);font-size:12px}.content-wrap{padding:14px 18px 18px}
    .table-shell{border:1px solid rgba(156,178,205,.1);border-radius:14px;overflow:auto;background:rgba(5,10,18,.42)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:11px 10px;text-align:left;border-bottom:1px solid rgba(156,178,205,.08);vertical-align:top}th{position:sticky;top:0;background:#102036;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;z-index:1}tbody tr{cursor:pointer}tbody tr:hover{background:rgba(119,168,255,.08)}tbody tr.active{background:rgba(100,210,177,.09)}
    .mono{font-family:var(--mono)}.subline{display:block;margin-top:4px;color:var(--muted);font-size:12px;line-height:1.35;overflow-wrap:anywhere}.num{white-space:nowrap}.actions{display:flex;gap:6px;flex-wrap:wrap}.path{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .state-pill{display:inline-flex;align-items:center;gap:6px;padding:5px 8px;border-radius:999px;background:rgba(119,168,255,.12);color:#d8e6ff;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
    .timeline-shell{border:1px solid rgba(156,178,205,.1);border-radius:16px;padding:12px 12px 6px;background:linear-gradient(180deg, rgba(7,13,22,.9), rgba(10,18,30,.9));overflow:auto}.timeline-chart{min-width:860px;height:300px;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(34px,1fr);gap:10px;align-items:end;padding:16px 6px 8px;border-bottom:1px solid rgba(156,178,205,.08)}
    .bucket{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:0}.bucket-btn{width:100%;height:220px;display:flex;align-items:flex-end;justify-content:center;border:none;background:transparent;padding:0;cursor:pointer}.bucket-stack{width:100%;max-width:42px;min-height:6px;border-radius:12px 12px 5px 5px;overflow:hidden;display:flex;flex-direction:column-reverse;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03);transition:transform .14s ease}.bucket-btn:hover .bucket-stack{transform:translateY(-2px)}.bucket.active .bucket-stack{outline:2px solid rgba(255,255,255,.22);outline-offset:4px}.segment{width:100%}.segment.output{background-image:repeating-linear-gradient(135deg, rgba(255,255,255,.28) 0 4px, transparent 4px 8px);background-blend-mode:screen;opacity:.94}.bucket-label,.bucket-total{font-size:11px;color:var(--muted);text-align:center;white-space:nowrap}
    .timeline-notes{padding:10px 4px 2px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}.legend{display:flex;gap:10px;flex-wrap:wrap}.legend-item{display:inline-flex;align-items:center;gap:6px}.swatch{width:10px;height:10px;border-radius:999px;display:inline-block}
    .detail-panel{padding:18px;position:sticky;top:74px}.detail-panel h3{margin:8px 0 0;font-size:24px;line-height:1.05;overflow-wrap:anywhere}.detail-panel p{color:var(--muted);overflow-wrap:anywhere}.chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.chip{display:inline-flex;align-items:center;gap:6px;padding:7px 10px;border-radius:999px;background:#0a1420;border:1px solid rgba(156,178,205,.12);font-size:12px;font-weight:800}.kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.kv-item{padding:12px;border-radius:12px;border:1px solid rgba(156,178,205,.1);background:rgba(6,12,21,.46);min-width:0}.kv-item .k{font-size:11px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.08em}.kv-item .v{margin-top:5px;font-size:14px;font-weight:800;overflow-wrap:anywhere}
    .detail-section{margin-top:18px}.detail-section h4{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.stack-list{display:flex;flex-direction:column;gap:10px}.stack-item{padding:12px;border-radius:12px;border:1px solid rgba(156,178,205,.1);background:rgba(6,12,21,.46)}.stack-item .title{font-weight:800;overflow-wrap:anywhere}.stack-item .meta{margin-top:4px;color:var(--muted);font-size:12px;line-height:1.4;overflow-wrap:anywhere}.command{padding:10px 12px;border-radius:10px;background:#09131f;border:1px solid rgba(156,178,205,.12);color:#d8e6ff;font-family:var(--mono);font-size:12px;overflow:auto;white-space:nowrap}.empty{padding:24px;color:var(--muted)}
    .toast{position:fixed;right:20px;bottom:20px;padding:10px 14px;border-radius:999px;background:rgba(8,16,27,.94);border:1px solid rgba(156,178,205,.18);color:var(--ink);box-shadow:0 16px 32px rgba(0,0,0,.28);opacity:0;pointer-events:none;transform:translateY(8px);transition:opacity .18s ease, transform .18s ease;font-size:12px;font-weight:800}.toast.show{opacity:1;transform:translateY(0)}
    @media (max-width:1280px){.filters{grid-template-columns:repeat(3,minmax(0,1fr))}.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.workspace{grid-template-columns:1fr}.detail-panel{position:static}}
    @media (max-width:760px){.shell{padding:14px}.filters,.metric-grid,.kv{grid-template-columns:1fr}.hero-copy h1{font-size:24px}}
  </style>
</head>
<body>
  <div class="topbar"><div class="topbar-inner"><div class="brand">Claude Session Dashboard<small>Claude Code sessions, projects, accounts, and timeline drill-down</small></div><div class="status"><span class="status-dot"></span><span id="navStatus">Loading local Claude data...</span></div></div></div>
  <div class="shell">
    <section class="panel control-panel">
      <div class="hero-copy">
        <div class="eyebrow">Explorer</div>
        <h1>Sessions first, timeline second, with Claude account tabs across the top.</h1>
        <p>Claude sessions are filtered locally from transcript, history, and telemetry data. Input includes cache read and cache creation. Output is assistant output.</p>
      </div>
      <div class="account-tabs" id="accountTabs"></div>
      <div class="filters">
        <div class="field"><label>Quick Range</label><div class="range-strip"><button class="chip-btn" data-preset="24h">24H</button><button class="chip-btn" data-preset="5d">5D</button><button class="chip-btn" data-preset="7d">7D</button><button class="chip-btn" data-preset="30d">30D</button><button class="chip-btn" data-preset="all">All</button></div></div>
        <div class="field"><label for="bucketMode">Timeline Bucket</label><select id="bucketMode"><option value="day">Day</option><option value="hour">Hour</option></select></div>
        <div class="field"><label for="rangeStart">Custom Start</label><input id="rangeStart" type="datetime-local"></div>
        <div class="field"><label for="rangeEnd">Custom End</label><input id="rangeEnd" type="datetime-local"></div>
        <div class="field"><label for="search">Search</label><input id="search" type="search" placeholder="project, path, session, prompt"></div>
        <div class="field"><label>Actions</label><div class="range-strip"><button class="primary-btn" id="refreshBtn">Refresh</button><button class="ghost-btn" id="customBtn">Use Custom</button></div></div>
        <div class="field"><label>Window</label><div class="subline" id="windowMeta" style="margin-top:0">-</div></div>
      </div>
      <div class="metric-grid" id="metrics"></div>
    </section>
    <section class="workspace">
      <div class="panel">
        <div class="panel-head"><div><div class="eyebrow">Views</div><h2 id="viewTitle">Sessions</h2></div><div class="view-tabs"><button class="tab-btn active" data-view="sessions">Sessions</button><button class="tab-btn" data-view="projects">Projects</button><button class="tab-btn" data-view="timeline">Timeline</button></div></div>
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
    function compact(value){ const n=Number(value||0); const abs=Math.abs(n); if(abs>=1_000_000_000) return `${(n/1_000_000_000).toFixed(abs>=10_000_000_000?0:1)}B`; if(abs>=1_000_000) return `${(n/1_000_000).toFixed(abs>=10_000_000?0:1)}M`; if(abs>=1_000) return `${(n/1_000).toFixed(abs>=10_000?0:1)}K`; return fmtInt.format(n); }
    function escapeHtml(value){ return String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'); }
    function toSeconds(value){ if (value === null || value === undefined || value === '') return null; if (typeof value === 'number') return value; const date = new Date(value); return Number.isNaN(date.getTime()) ? null : date.getTime() / 1000; }
    function formatStamp(value, kind='datetime'){ const seconds = toSeconds(value); if (!seconds) return '-'; const date = new Date(seconds * 1000); return kind === 'date' ? fmtDateOnly.format(date) : fmtDateTime.format(date); }
    function localInputValue(seconds){ if (!seconds) return ''; const date = new Date(seconds * 1000); const y=date.getFullYear(); const m=String(date.getMonth()+1).padStart(2,'0'); const d=String(date.getDate()).padStart(2,'0'); const h=String(date.getHours()).padStart(2,'0'); const mm=String(date.getMinutes()).padStart(2,'0'); return `${y}-${m}-${d}T${h}:${mm}`; }
    function inputTokens(usage){ return Number((usage || {}).input_tokens || 0) + Number((usage || {}).cache_read_input_tokens || 0) + Number((usage || {}).cache_creation_input_tokens || 0); }
    function outputTokens(usage){ return Number((usage || {}).output_tokens || 0); }
    function totalTokens(usage){ return Number((usage || {}).total_tokens || 0) || inputTokens(usage) + outputTokens(usage); }
    function usageLine(usage){ return `${compact(inputTokens(usage))} in | ${compact(outputTokens(usage))} out`; }
    function usageMeta(usage){ const cache=(Number((usage||{}).cache_read_input_tokens||0)+Number((usage||{}).cache_creation_input_tokens||0)); const parts=[]; if(cache) parts.push(`cache ${compact(cache)}`); parts.push(`total ${compact(totalTokens(usage))}`); return parts.join(' | '); }
    function projectColor(name){ let hash=0; for(const ch of String(name||'unknown')) hash=((hash<<5)-hash)+ch.charCodeAt(0); return projectPalette[Math.abs(hash)%projectPalette.length]; }
    function showToast(message){ el('toast').textContent = message; el('toast').classList.add('show'); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => el('toast').classList.remove('show'), 1800); }
    async function copyText(value, label){ try { await navigator.clipboard.writeText(value); showToast(`${label} copied`); } catch (err) { showToast('Clipboard unavailable'); } }
    function nowSeconds(){ return Date.now() / 1000; }
    function currentRange(){ const end = toSeconds(el('rangeEnd').value) || nowSeconds(); const customStart = toSeconds(el('rangeStart').value); const map = { '24h':86400, '5d':432000, '7d':604800, '30d':2592000 }; if (state.preset === 'custom') return {start: customStart || end - 604800, end}; if (state.preset === 'all') { const rows = activitySource(); const starts = rows.map(row => Number(row.bucket_start || 0)).filter(Boolean); return {start: Math.min(...starts, end - 604800), end: Math.max(...starts, end)}; } const delta = map[state.preset] || 604800; return {start: end - delta, end}; }
    function syncRangeInputs(){ const range = currentRange(); if (!el('rangeStart').value || state.preset !== 'custom') { el('rangeStart').value = localInputValue(range.start); el('rangeEnd').value = localInputValue(range.end); } document.querySelectorAll('[data-preset]').forEach(btn => btn.classList.toggle('active', btn.dataset.preset === state.preset)); }
    function activitySource(){ return state.snapshot?.activity?.[el('bucketMode').value === 'hour' ? 'hour_rows' : 'day_rows'] || []; }
    function filteredActivityRows(){ const {start,end} = currentRange(); return activitySource().filter(row => (state.selectedAccount === 'all' || (row.account_key || 'unknown') === state.selectedAccount) && Number(row.bucket_start || 0) >= start && Number(row.bucket_start || 0) <= end); }
    function sessionWindowMap(){ const map = {}; for (const row of filteredActivityRows()) { const current = map[row.session_id] || {input_tokens:0, output_tokens:0, cache_read_input_tokens:0, cache_creation_input_tokens:0, total_tokens:0, assistant_messages:0, user_prompts:0, errors:0}; current.input_tokens += Number(row.input_tokens || 0); current.output_tokens += Number(row.output_tokens || 0); current.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); current.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); current.total_tokens += Number(row.total_tokens || 0); current.assistant_messages += Number(row.assistant_messages || 0); current.user_prompts += Number(row.user_prompts || 0); current.errors += Number(row.errors || 0); map[row.session_id] = current; } return map; }
    function normalizedSessions(){ const windowMap = sessionWindowMap(); const {start,end} = currentRange(); const q = state.search.trim().toLowerCase(); return (state.snapshot?.sessions || []).filter(session => state.selectedAccount === 'all' || (session.account_key || 'unknown') === state.selectedAccount).filter(session => state.preset === 'all' || windowMap[session.session_id] || ((session.last_access_at || 0) >= start && (session.last_access_at || 0) <= end)).map(session => ({...session, window_usage: windowMap[session.session_id] || {input_tokens:0, output_tokens:0, cache_read_input_tokens:0, cache_creation_input_tokens:0, total_tokens:0}, working_path: session.working_path || session.project_path || session.session_dir})).filter(session => !q || `${session.session_id} ${session.project_path || ''} ${session.working_path || ''} ${session.last_prompt || ''}`.toLowerCase().includes(q)).sort((a,b) => (b.last_access_at || 0) - (a.last_access_at || 0) || totalTokens(b.window_usage) - totalTokens(a.window_usage)); }
    function projectId(row){ return `${row.account_key || 'unknown'}::${row.project_path || row.project_key || '-'}`; }
    function normalizedProjects(){ const map = {}; for (const session of normalizedSessions()) { const key = `${session.account_key || 'unknown'}::${session.project_path || session.project_key || '-'}`; const current = map[key] || { project_id:key, project_path:session.project_path || session.project_key || '-', project_key:session.project_key, account_key:session.account_key || 'unknown', account_label:session.account_label || 'Unknown', last_access_at:0, session_count:0, lifetime_usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}, window_usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}, top_session:'', top_total:0 }; current.session_count += 1; current.last_access_at = Math.max(current.last_access_at, session.last_access_at || 0); current.lifetime_usage.input_tokens += Number(session.tokens?.input_tokens || 0); current.lifetime_usage.output_tokens += Number(session.tokens?.output_tokens || 0); current.lifetime_usage.cache_read_input_tokens += Number(session.tokens?.cache_read_input_tokens || 0); current.lifetime_usage.cache_creation_input_tokens += Number(session.tokens?.cache_creation_input_tokens || 0); current.lifetime_usage.total_tokens += Number(session.tokens?.total_tokens || 0); current.window_usage.input_tokens += Number(session.window_usage?.input_tokens || 0); current.window_usage.output_tokens += Number(session.window_usage?.output_tokens || 0); current.window_usage.cache_read_input_tokens += Number(session.window_usage?.cache_read_input_tokens || 0); current.window_usage.cache_creation_input_tokens += Number(session.window_usage?.cache_creation_input_tokens || 0); current.window_usage.total_tokens += Number(session.window_usage?.total_tokens || 0); if (totalTokens(session.window_usage) >= current.top_total) { current.top_total = totalTokens(session.window_usage); current.top_session = session.session_id; } map[key] = current; } return Object.values(map).sort((a,b) => b.last_access_at - a.last_access_at || totalTokens(b.window_usage) - totalTokens(a.window_usage)); }
    function timelineRows(){ const sessionsById = Object.fromEntries(normalizedSessions().map(session => [session.session_id, session])); const projectsById = Object.fromEntries(normalizedProjects().map(project => [project.project_id, project])); const buckets = {}; for (const row of filteredActivityRows()) { const key = String(row.bucket_start); const current = buckets[key] || { bucket_start:Number(row.bucket_start), usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}, session_count:0, project_count:0, session_map:{}, project_map:{} }; current.usage.input_tokens += Number(row.input_tokens || 0); current.usage.output_tokens += Number(row.output_tokens || 0); current.usage.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); current.usage.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); current.usage.total_tokens += Number(row.total_tokens || 0); const sessionEntry = current.session_map[row.session_id] || { session_id:row.session_id, label:row.session_id, project:row.project_path || row.project_key || '-', cwd:(sessionsById[row.session_id] || {}).working_path || row.project_path || '-', usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0} }; sessionEntry.label = (sessionsById[row.session_id] || {}).session_id || row.session_id; sessionEntry.usage.input_tokens += Number(row.input_tokens || 0); sessionEntry.usage.output_tokens += Number(row.output_tokens || 0); sessionEntry.usage.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); sessionEntry.usage.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); sessionEntry.usage.total_tokens += Number(row.total_tokens || 0); current.session_map[row.session_id] = sessionEntry; const pKey = `${row.account_key || 'unknown'}::${row.project_path || row.project_key || '-'}`; const projectEntry = current.project_map[pKey] || { project_id:pKey, project:row.project_path || row.project_key || '-', cwd:row.project_path || '-', usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0} }; projectEntry.usage.input_tokens += Number(row.input_tokens || 0); projectEntry.usage.output_tokens += Number(row.output_tokens || 0); projectEntry.usage.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); projectEntry.usage.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); projectEntry.usage.total_tokens += Number(row.total_tokens || 0); current.project_map[pKey] = projectEntry; buckets[key] = current; }
      return Object.values(buckets).sort((a,b) => a.bucket_start - b.bucket_start).map(bucket => ({ ...bucket, session_rows:Object.values(bucket.session_map).sort((a,b)=>totalTokens(b.usage)-totalTokens(a.usage)), project_rows:Object.values(bucket.project_map).sort((a,b)=>totalTokens(b.usage)-totalTokens(a.usage)), session_count:Object.keys(bucket.session_map).length, project_count:Object.keys(bucket.project_map).length }));
    }
    function currentBucket(){ const rows = timelineRows(); return rows.find(row => row.bucket_start === state.selectedBucketStart) || rows[rows.length - 1] || null; }
    function timelineBreakdownRows(){ const bucket = currentBucket(); if (!bucket) return []; const q = state.search.trim().toLowerCase(); let rows = state.timelineMode === 'projects' ? [...bucket.project_rows] : [...bucket.session_rows]; if (q) rows = rows.filter(row => `${row.project || ''} ${row.label || ''} ${row.cwd || ''} ${row.session_id || ''}`.toLowerCase().includes(q)); return rows; }
    function accountRows(){ const base = state.snapshot?.accounts || []; const rows = filteredActivityRows(); const allTotals = {input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}; for (const row of rows) { allTotals.input_tokens += Number(row.input_tokens || 0); allTotals.output_tokens += Number(row.output_tokens || 0); allTotals.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); allTotals.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); allTotals.total_tokens += Number(row.total_tokens || 0); } const allSessions = new Set(normalizedSessions().map(session => session.session_id)); const summaryRows = [{ account_key:'all', label:'All Accounts', session_count:(state.snapshot?.sessions || []).length, project_count:normalizedProjects().length, latest_access_at:Math.max(...(state.snapshot?.sessions || []).map(session => session.last_access_at || 0), 0), window_usage:allTotals, window_session_count:allSessions.size }]; for (const account of base) { const accountRows = rows.filter(row => (row.account_key || 'unknown') === account.account_key); const totals = {input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}; for (const row of accountRows) { totals.input_tokens += Number(row.input_tokens || 0); totals.output_tokens += Number(row.output_tokens || 0); totals.cache_read_input_tokens += Number(row.cache_read_input_tokens || 0); totals.cache_creation_input_tokens += Number(row.cache_creation_input_tokens || 0); totals.total_tokens += Number(row.total_tokens || 0); } summaryRows.push({ ...account, window_usage: totals, window_session_count: new Set(accountRows.map(row => row.session_id)).size }); } return summaryRows; }
    function ensureSelection(){ const sessions = normalizedSessions(); if (!sessions.find(row => row.session_id === state.selectedSessionId)) state.selectedSessionId = sessions[0]?.session_id || null; const projects = normalizedProjects(); if (!projects.find(row => row.project_id === state.selectedProjectId)) state.selectedProjectId = projects[0]?.project_id || null; const buckets = timelineRows(); if (!buckets.find(row => row.bucket_start === state.selectedBucketStart)) state.selectedBucketStart = buckets[buckets.length - 1]?.bucket_start || null; }
    function renderAccountTabs(){ el('accountTabs').innerHTML = accountRows().map(row => `<button class="account-tab ${row.account_key === state.selectedAccount ? 'active' : ''}" data-account="${escapeHtml(row.account_key)}"><span class="title">${escapeHtml(row.label)}</span><span class="meta">${compact(inputTokens(row.window_usage))} in | ${compact(outputTokens(row.window_usage))} out | ${fmtInt.format(row.window_session_count || row.session_count || 0)} sessions</span></button>`).join(''); }
    function renderMetrics(){ const sessions = normalizedSessions(); const projects = normalizedProjects(); const totals = sessions.reduce((acc, session) => { acc.input_tokens += Number(session.window_usage?.input_tokens || 0); acc.output_tokens += Number(session.window_usage?.output_tokens || 0); acc.cache_read_input_tokens += Number(session.window_usage?.cache_read_input_tokens || 0); acc.cache_creation_input_tokens += Number(session.window_usage?.cache_creation_input_tokens || 0); acc.total_tokens += Number(session.window_usage?.total_tokens || 0); return acc; }, {input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0,total_tokens:0}); const latest = Math.max(...sessions.map(session => session.last_access_at || 0), 0); const account = accountRows().find(row => row.account_key === state.selectedAccount); const cards = [['Input', compact(inputTokens(totals)), `Cache ${compact((totals.cache_read_input_tokens || 0) + (totals.cache_creation_input_tokens || 0))}`], ['Output', compact(outputTokens(totals)), `Window total ${compact(totalTokens(totals))}`], ['Sessions', fmtInt.format(sessions.length), `${fmtInt.format(projects.length)} projects`], ['Latest', formatStamp(latest), `${el('bucketMode').value} buckets`], ['Account', account ? account.label : 'All Accounts', `${compact(totalTokens((account || {}).window_usage || totals))} total`], ['Source', 'Ready', state.snapshot?.sources?.root_dir || '-']]; el('metrics').innerHTML = cards.map(([label, value, meta]) => `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div><div class="meta">${escapeHtml(meta)}</div></div>`).join(''); const range = currentRange(); el('windowMeta').textContent = `${formatStamp(range.start)} -> ${formatStamp(range.end)} (${state.preset})`; }
    function renderSessionsView(){ const rows = normalizedSessions(); el('viewTitle').textContent = 'Sessions'; el('contentMeta').textContent = `${fmtInt.format(rows.length)} sessions in scope. Click a row for details. Resume and path actions copy commands.`; if (!rows.length) { el('mainContent').innerHTML = '<div class="empty">No Claude sessions matched the current account, window, and search filters.</div>'; return; } el('mainContent').innerHTML = `<div class="table-shell"><table><thead><tr><th>Session</th><th>Project / Path</th><th>Last Used</th><th>Input</th><th>Output</th><th>Total</th><th>Resume</th><th>Status</th></tr></thead><tbody>${rows.map(row => `<tr class="${row.session_id === state.selectedSessionId ? 'active' : ''}" data-select-session="${escapeHtml(row.session_id)}"><td><strong class="mono">${escapeHtml(row.session_id.slice(0, 8))}</strong><span class="subline mono">${escapeHtml(row.session_id)}</span><span class="subline">${escapeHtml(formatStamp(row.first_event_at || row.last_access_at))}</span></td><td><strong>${escapeHtml(row.project_path || row.project_key || '-')}</strong><span class="subline mono path">${escapeHtml(row.working_path || row.session_dir || '-')}</span><span class="subline">${escapeHtml(row.account_label || 'Unknown account')}</span></td><td>${escapeHtml(formatStamp(row.last_access_at))}<span class="subline">${escapeHtml(String(row.message_counts?.user_prompts || 0))} prompts | ${escapeHtml(String(row.message_counts?.assistant_messages || 0))} assistant</span></td><td class="num">${compact(inputTokens(row.window_usage))}<span class="subline">${compact((row.window_usage?.cache_read_input_tokens || 0) + (row.window_usage?.cache_creation_input_tokens || 0))} cache</span></td><td class="num">${compact(outputTokens(row.window_usage))}<span class="subline">${compact(row.window_usage?.assistant_messages || 0)} msgs</span></td><td class="num">${compact(totalTokens(row.window_usage))}<span class="subline">${escapeHtml(usageMeta(row.tokens))}</span></td><td><div class="actions"><button class="tiny-btn" data-copy-label="Resume command" data-copy="${escapeHtml(row.resume_command || '')}">Resume</button><button class="tiny-btn" data-copy-label="Path" data-copy="${escapeHtml(row.working_path || row.session_dir || '')}">Path</button>${row.open_command ? `<button class="tiny-btn" data-copy-label="Open command" data-copy="${escapeHtml(row.open_command)}">Open</button>` : ''}</div></td><td><span class="state-pill">${escapeHtml(row.state || 'unknown')}</span><span class="subline">${escapeHtml(row.last_access_relative || '-')}</span></td></tr>`).join('')}</tbody></table></div>`; }
    function renderProjectsView(){ const rows = normalizedProjects(); el('viewTitle').textContent = 'Projects'; el('contentMeta').textContent = `${fmtInt.format(rows.length)} projects in scope. Project rows aggregate matching sessions in the selected account and range.`; if (!rows.length) { el('mainContent').innerHTML = '<div class="empty">No Claude projects matched the current filters.</div>'; return; } el('mainContent').innerHTML = `<div class="table-shell"><table><thead><tr><th>Project</th><th>Account</th><th>Sessions</th><th>Last Used</th><th>Input</th><th>Output</th><th>Total</th></tr></thead><tbody>${rows.map(row => `<tr class="${row.project_id === state.selectedProjectId ? 'active' : ''}" data-select-project="${escapeHtml(row.project_id)}"><td><strong>${escapeHtml(row.project_path)}</strong><span class="subline mono">${escapeHtml(row.project_id)}</span><span class="subline">Top session: ${escapeHtml(row.top_session || '-')}</span></td><td>${escapeHtml(row.account_label || 'Unknown')}</td><td class="num">${fmtInt.format(row.session_count || 0)}</td><td>${escapeHtml(formatStamp(row.last_access_at))}</td><td class="num">${compact(inputTokens(row.window_usage))}</td><td class="num">${compact(outputTokens(row.window_usage))}</td><td class="num">${compact(totalTokens(row.window_usage))}<span class="subline">${escapeHtml(usageMeta(row.lifetime_usage))}</span></td></tr>`).join('')}</tbody></table></div>`; }
    function bucketSegments(row){ const segments = []; for (const item of row.project_rows || []) { const inTokens = inputTokens(item.usage); const outTokens = outputTokens(item.usage); if (inTokens) segments.push({project:item.project, tokenType:'input', tokens:inTokens, color:projectColor(item.project)}); if (outTokens) segments.push({project:item.project, tokenType:'output', tokens:outTokens, color:projectColor(item.project)}); } return segments; }
    function renderTimelineView(){ const rows = timelineRows(); el('viewTitle').textContent = 'Timeline'; if (!rows.length) { el('contentMeta').textContent = 'No bucketed activity exists in the selected window.'; el('mainContent').innerHTML = '<div class="empty">No token activity landed in the selected range.</div>'; return; } const current = currentBucket(); const maxTotal = Math.max(...rows.map(row => totalTokens(row.usage)), 1); const breakdown = timelineBreakdownRows(); const legendProjects = [...new Set(rows.flatMap(row => row.project_rows.slice(0, 3).map(item => item.project)).filter(Boolean))].slice(0, 6); el('contentMeta').textContent = `${fmtInt.format(rows.length)} ${el('bucketMode').value} buckets on the x-axis. Click a bucket to inspect sessions or projects behind that time slice.`; el('mainContent').innerHTML = `<div class="timeline-shell"><div class="timeline-chart">${rows.map(row => { const total = totalTokens(row.usage); const scaledHeight = Math.max(6, Math.round((total / maxTotal) * 220)); const segments = bucketSegments(row); return `<div class="bucket ${row.bucket_start === state.selectedBucketStart ? 'active' : ''}"><div class="bucket-total">${escapeHtml(compact(total))}</div><button class="bucket-btn" data-select-bucket="${escapeHtml(String(row.bucket_start))}" title="${escapeHtml(formatStamp(row.bucket_start, el('bucketMode').value === 'day' ? 'date' : 'datetime'))}: ${escapeHtml(usageLine(row.usage))}"><div class="bucket-stack" style="height:${scaledHeight}px">${segments.map(segment => `<div class="segment ${segment.tokenType === 'output' ? 'output' : ''}" style="height:${Math.max(2, (segment.tokens / Math.max(total,1)) * 100)}%; background:${segment.color}" title="${escapeHtml(segment.project)} | ${segment.tokenType} | ${escapeHtml(compact(segment.tokens))}"></div>`).join('')}</div></button><div class="bucket-label">${escapeHtml(formatStamp(row.bucket_start, el('bucketMode').value === 'day' ? 'date' : 'datetime'))}</div></div>`; }).join('')}</div><div class="timeline-notes"><div class="legend"><span class="legend-item"><span class="swatch" style="background:#bfe7db"></span>Input segments</span><span class="legend-item"><span class="swatch" style="background:#bfe7db;background-image:repeating-linear-gradient(135deg, rgba(255,255,255,.75) 0 4px, transparent 4px 8px)"></span>Output segments</span>${legendProjects.map(project => `<span class="legend-item"><span class="swatch" style="background:${projectColor(project)}"></span>${escapeHtml(project)}</span>`).join('')}</div><div>${escapeHtml(formatStamp(current?.bucket_start, el('bucketMode').value === 'day' ? 'date' : 'datetime'))} | ${escapeHtml(usageLine((current || {}).usage || {}))}</div></div></div><div style="height:12px"></div><div class="panel" style="background:rgba(0,0,0,.08);box-shadow:none"><div class="panel-head" style="padding:12px 14px"><div><div class="eyebrow">Bucket Breakdown</div><h2 style="font-size:16px;margin-top:4px">${escapeHtml(formatStamp(current?.bucket_start, el('bucketMode').value === 'day' ? 'date' : 'datetime'))}</h2></div><div class="timeline-mode"><button class="tab-btn ${state.timelineMode === 'sessions' ? 'active' : ''}" data-timeline-mode="sessions">Sessions</button><button class="tab-btn ${state.timelineMode === 'projects' ? 'active' : ''}" data-timeline-mode="projects">Projects</button></div></div><div class="content-wrap"><div class="table-shell"><table><thead><tr><th>${state.timelineMode === 'projects' ? 'Project' : 'Session'}</th><th>Path</th><th>Input</th><th>Output</th><th>Total</th><th>Jump</th></tr></thead><tbody>${breakdown.map(row => `<tr ${state.timelineMode === 'projects' ? `data-jump-project="${escapeHtml(row.project_id || '')}"` : `data-jump-session="${escapeHtml(row.session_id || '')}"`}><td><strong>${escapeHtml(row.project || row.label || '-')}</strong>${state.timelineMode === 'projects' ? '' : `<span class="subline mono">${escapeHtml(row.session_id || '-')}</span>`}</td><td><span class="subline mono">${escapeHtml(row.cwd || row.project || '-')}</span></td><td class="num">${compact(inputTokens(row.usage))}</td><td class="num">${compact(outputTokens(row.usage))}</td><td class="num">${compact(totalTokens(row.usage))}</td><td>${state.timelineMode === 'projects' ? 'Open project' : 'Open session'}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">No rows matched the search filter for this bucket.</td></tr>'}</tbody></table></div></div></div>`; }
    function renderDetail(){ if (state.view === 'projects') { const project = normalizedProjects().find(row => row.project_id === state.selectedProjectId); if (!project) { el('detailPanel').innerHTML = '<div class="eyebrow">Project Detail</div><h3>No project selected</h3><p>Choose a project row to inspect related Claude sessions.</p>'; return; } const related = normalizedSessions().filter(row => `${row.account_key || 'unknown'}::${row.project_path || row.project_key || '-'}` === project.project_id).slice(0, 8); el('detailPanel').innerHTML = `<div class="eyebrow">Project Detail</div><h3>${escapeHtml(project.project_path)}</h3><p>${escapeHtml(project.account_label || 'Unknown account')}</p><div class="chips"><span class="chip">${fmtInt.format(project.session_count || 0)} sessions</span><span class="chip">${compact(inputTokens(project.window_usage))} in</span><span class="chip">${compact(outputTokens(project.window_usage))} out</span><span class="chip">${compact(totalTokens(project.window_usage))} total</span></div><div class="kv"><div class="kv-item"><div class="k">Last Used</div><div class="v">${escapeHtml(formatStamp(project.last_access_at))}</div></div><div class="kv-item"><div class="k">Top Session</div><div class="v">${escapeHtml(project.top_session || '-')}</div></div><div class="kv-item"><div class="k">Lifetime Input</div><div class="v">${compact(inputTokens(project.lifetime_usage))}</div></div><div class="kv-item"><div class="k">Lifetime Output</div><div class="v">${compact(outputTokens(project.lifetime_usage))}</div></div></div><div class="detail-section"><h4>Recent Sessions</h4><div class="stack-list">${related.map(row => `<div class="stack-item"><div class="title mono">${escapeHtml(row.session_id)}</div><div class="meta">${escapeHtml(formatStamp(row.last_access_at))} • ${escapeHtml(usageLine(row.window_usage))} • ${escapeHtml(row.state || 'unknown')}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No related sessions in this view.</div></div>'}</div></div>`; return; } if (state.view === 'timeline') { const bucket = currentBucket(); if (!bucket) { el('detailPanel').innerHTML = '<div class="eyebrow">Timeline Detail</div><h3>No bucket selected</h3><p>Select a bucket in the chart to inspect that day or hour.</p>'; return; } el('detailPanel').innerHTML = `<div class="eyebrow">Timeline Detail</div><h3>${escapeHtml(formatStamp(bucket.bucket_start, el('bucketMode').value === 'day' ? 'date' : 'datetime'))}</h3><p>${fmtInt.format(bucket.session_count || 0)} sessions • ${fmtInt.format(bucket.project_count || 0)} projects</p><div class="chips"><span class="chip">${compact(inputTokens(bucket.usage))} in</span><span class="chip">${compact(outputTokens(bucket.usage))} out</span><span class="chip">${compact(totalTokens(bucket.usage))} total</span></div><div class="detail-section"><h4>Top Projects</h4><div class="stack-list">${(bucket.project_rows || []).slice(0,6).map(row => `<div class="stack-item"><div class="title">${escapeHtml(row.project)}</div><div class="meta">${escapeHtml(usageLine(row.usage))} • ${escapeHtml(usageMeta(row.usage))}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No project rows for this bucket.</div></div>'}</div></div><div class="detail-section"><h4>Top Sessions</h4><div class="stack-list">${(bucket.session_rows || []).slice(0,6).map(row => `<div class="stack-item"><div class="title mono">${escapeHtml(row.session_id)}</div><div class="meta">${escapeHtml(row.project || '-')} • ${escapeHtml(usageLine(row.usage))}</div></div>`).join('') || '<div class="stack-item"><div class="meta">No session rows for this bucket.</div></div>'}</div></div>`; return; } const session = normalizedSessions().find(row => row.session_id === state.selectedSessionId); if (!session) { el('detailPanel').innerHTML = '<div class="eyebrow">Session Detail</div><h3>No session selected</h3><p>Select a session to inspect path, resume command, and prompt context.</p>'; return; } el('detailPanel').innerHTML = `<div class="eyebrow">Session Detail</div><h3 class="mono">${escapeHtml(session.session_id)}</h3><p>${escapeHtml(session.project_path || session.project_key || '-')} • ${escapeHtml(formatStamp(session.last_access_at))}</p><div class="chips"><span class="chip">${compact(inputTokens(session.window_usage))} in</span><span class="chip">${compact(outputTokens(session.window_usage))} out</span><span class="chip">${compact(totalTokens(session.window_usage))} total</span><span class="chip">${escapeHtml(session.state || 'unknown')}</span></div><div class="kv"><div class="kv-item"><div class="k">Working Path</div><div class="v mono">${escapeHtml(session.working_path || session.session_dir || '-')}</div></div><div class="kv-item"><div class="k">Duration</div><div class="v">${escapeHtml(String(session.duration_minutes || 0))} min</div></div><div class="kv-item"><div class="k">First Event</div><div class="v">${escapeHtml(formatStamp(session.first_event_at))}</div></div><div class="kv-item"><div class="k">Last Event</div><div class="v">${escapeHtml(formatStamp(session.last_event_at))}</div></div><div class="kv-item"><div class="k">Prompts</div><div class="v">${escapeHtml(String(session.message_counts?.user_prompts || 0))}</div></div><div class="kv-item"><div class="k">Assistant Messages</div><div class="v">${escapeHtml(String(session.message_counts?.assistant_messages || 0))}</div></div></div><div class="detail-section"><h4>Resume Command</h4><div class="command">${escapeHtml(session.resume_command || '-')}</div><div style="height:8px"></div><div class="actions"><button class="tiny-btn" data-copy-label="Resume command" data-copy="${escapeHtml(session.resume_command || '')}">Copy Resume</button><button class="tiny-btn" data-copy-label="Path" data-copy="${escapeHtml(session.working_path || session.session_dir || '')}">Copy Path</button>${session.open_command ? `<button class="tiny-btn" data-copy-label="Open command" data-copy="${escapeHtml(session.open_command)}">Copy Open</button>` : ''}</div></div><div class="detail-section"><h4>Last Prompt</h4><div class="stack-list"><div class="stack-item"><div class="meta">${escapeHtml(session.last_prompt || '-')}</div></div></div></div><div class="detail-section"><h4>Files</h4><div class="stack-list"><div class="stack-item"><div class="title">Session File</div><div class="meta mono">${escapeHtml(session.session_file || '-')}</div></div><div class="stack-item"><div class="title">Session Folder</div><div class="meta mono">${escapeHtml(session.session_dir || '-')}</div></div></div></div>`; }
    function renderMain(){ if (state.view === 'projects') renderProjectsView(); else if (state.view === 'timeline') renderTimelineView(); else renderSessionsView(); }
    function renderAll(){ syncRangeInputs(); renderAccountTabs(); renderMetrics(); renderMain(); renderDetail(); }
    async function loadSnapshot(){ el('navStatus').textContent = 'Refreshing local Claude session data...'; state.snapshot = await (await fetch('/api/state')).json(); const known = new Set(['all', ...(state.snapshot?.accounts || []).map(account => account.account_key)]); if (!known.has(state.selectedAccount)) state.selectedAccount = 'all'; ensureSelection(); renderAll(); el('navStatus').textContent = `${formatStamp(state.snapshot?.generated_at)} | ${state.snapshot?.sources?.root_dir || '~/.claude'}`; }
    document.body.addEventListener('click', async (event) => {
      const copyBtn = event.target.closest('[data-copy]'); if (copyBtn) { event.stopPropagation(); await copyText(copyBtn.dataset.copy || '', copyBtn.dataset.copyLabel || 'Value'); return; }
      const accountBtn = event.target.closest('[data-account]'); if (accountBtn) { state.selectedAccount = accountBtn.dataset.account; ensureSelection(); renderAll(); return; }
      const presetBtn = event.target.closest('[data-preset]'); if (presetBtn) { state.preset = presetBtn.dataset.preset; ensureSelection(); renderAll(); return; }
      const viewBtn = event.target.closest('[data-view]'); if (viewBtn) { state.view = viewBtn.dataset.view; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); ensureSelection(); renderMain(); renderDetail(); return; }
      const bucketBtn = event.target.closest('[data-select-bucket]'); if (bucketBtn) { state.selectedBucketStart = Number(bucketBtn.dataset.selectBucket); renderMain(); renderDetail(); return; }
      const sessionBtn = event.target.closest('[data-select-session]'); if (sessionBtn) { state.selectedSessionId = sessionBtn.dataset.selectSession; renderMain(); renderDetail(); return; }
      const projectBtn = event.target.closest('[data-select-project]'); if (projectBtn) { state.selectedProjectId = projectBtn.dataset.selectProject; renderMain(); renderDetail(); return; }
      const jumpSession = event.target.closest('[data-jump-session]'); if (jumpSession) { state.view = 'sessions'; state.selectedSessionId = jumpSession.dataset.jumpSession; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); renderMain(); renderDetail(); return; }
      const jumpProject = event.target.closest('[data-jump-project]'); if (jumpProject) { state.view = 'projects'; state.selectedProjectId = jumpProject.dataset.jumpProject; document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === state.view)); renderMain(); renderDetail(); return; }
      const timelineModeBtn = event.target.closest('[data-timeline-mode]'); if (timelineModeBtn) { state.timelineMode = timelineModeBtn.dataset.timelineMode; renderMain(); renderDetail(); }
    });
    el('refreshBtn').addEventListener('click', () => loadSnapshot().catch(err => showToast(err.message)));
    el('customBtn').addEventListener('click', () => { state.preset = 'custom'; ensureSelection(); renderAll(); });
    el('bucketMode').addEventListener('change', () => { ensureSelection(); renderAll(); });
    el('search').addEventListener('input', (event) => { state.search = event.target.value || ''; ensureSelection(); renderMain(); renderDetail(); });
    loadSnapshot().catch(err => { el('navStatus').textContent = 'Load failed'; el('mainContent').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`; el('detailPanel').innerHTML = `<div class="eyebrow">Status</div><h3>Unable to load dashboard</h3><p>${escapeHtml(err.message)}</p>`; });
  </script>
</body>
</html>
"""


class ClaudeDashboardApp:
    def __init__(self, root_dir: Path):
        self.lock = threading.RLock()
        self.analyzer = ClaudeSessionAnalyzer(root_dir)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.analyzer.snapshot()


class ClaudeDashboardHandler(BaseHTTPRequestHandler):
    server: "ClaudeDashboardServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(html_page_v2())
            return
        if parsed.path == "/api/state":
            try:
                self._send_json(self.server.app.snapshot())
            except Exception as exc:
                self._send_json({"error": compact_error(exc)}, status=500)
            return
        self._send_json({"error": "Not found"}, status=404)


class ClaudeDashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app: ClaudeDashboardApp):
        super().__init__(server_address, ClaudeDashboardHandler)
        self.app = app


def main() -> int:
    args = parse_args()
    app = ClaudeDashboardApp(args.root)
    server = ClaudeDashboardServer((args.host, args.port), app)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Claude Session Dashboard listening at {url}", flush=True)
    print(f"Root: {args.root.expanduser()}", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
