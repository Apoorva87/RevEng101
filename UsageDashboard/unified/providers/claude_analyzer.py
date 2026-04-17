"""Claude Code session analyzer — reads ~/.claude project JSONL files."""

from __future__ import annotations

import json
import shlex
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
DEFAULT_ROOT = Path.home() / ".claude"

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


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
    cutoff = datetime.now(tz=LOCAL_TZ) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    return sum(
        r.get(field, 0)
        for r in rows
        if (r.get("ts") or 0) >= cutoff_ts
    )


def safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def numeric_usage_totals(usage: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in USAGE_FIELDS:
        val = usage.get(field, 0)
        try:
            result[field] = int(val or 0)
        except (TypeError, ValueError):
            result[field] = 0
    result["total_tokens"] = sum(result.values())
    result["read_tokens"] = result.get("input_tokens", 0) + result.get("cache_read_input_tokens", 0)
    result["write_tokens"] = result.get("output_tokens", 0) + result.get("cache_creation_input_tokens", 0)
    return result


def merge_counter(target: dict[str, int], source: dict[str, int]) -> None:
    for k, v in source.items():
        target[k] = target.get(k, 0) + v


def is_tool_result_only(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return all(
        isinstance(item, dict) and item.get("type") == "tool_result"
        for item in content
    )


def extract_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    t = item.get("text", "")
                    if t:
                        parts.append(t)
        return " ".join(parts).strip() or None
    return None


def extract_error_text(entry: dict[str, Any]) -> str | None:
    msg = entry.get("message") or {}
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                for sub in item.get("content", []):
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        t = sub.get("text", "")
                        if "error" in t.lower() or "failed" in t.lower():
                            return t[:200]
    return None


@dataclass
class SessionParseResult:
    session_id: str
    project_path: str | None
    working_path: str | None
    account_key: str | None
    account_label: str | None
    tokens: dict[str, int]
    state: str
    first_event_at: float | None
    last_access_at: float | None
    last_event_at: float | None
    last_prompt: str | None
    last_error_text: str | None
    resume_command: str
    open_command: str | None
    duration_seconds: int
    models: list[str]
    usage_events: list[dict[str, Any]]
    limit_events: list[dict[str, Any]]
    user_prompts: int
    assistant_messages: int
    tool_uses: int
    file_mtime: float | None
    session_file: str | None


class ClaudeSessionAnalyzer:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.expanduser()
        self.projects_dir = self.root_dir / "projects"
        self.history_path = self.root_dir / "history.jsonl"
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _history_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.history_path.stat()
            return (int(stat.st_mtime * 1000), stat.st_size)
        except FileNotFoundError:
            return None

    def _scan_history(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not self.history_path.exists():
            return result
        try:
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = entry.get("sessionId")
                if not session_id:
                    continue
                ts = parse_iso_ts(entry.get("timestamp"))
                existing = result.get(session_id)
                if existing is None or (ts and (existing.get("ts") or 0) < ts):
                    result[session_id] = {
                        "ts": ts,
                        "display": entry.get("display") or entry.get("summary") or "",
                        "raw": entry,
                    }
        except Exception:
            pass
        return result

    def _session_signature(self, path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
            return (int(stat.st_mtime * 1000), stat.st_size)
        except FileNotFoundError:
            return (0, 0)

    def _bucket_dict(self) -> dict[str, int]:
        return {f: 0 for f in USAGE_FIELDS}

    def _telemetry_signature(self) -> tuple[tuple[str, int, int], ...]:
        telemetry_dir = self.root_dir / "statsig"
        if not telemetry_dir.exists():
            return ()
        entries = []
        for p in sorted(telemetry_dir.iterdir()):
            try:
                stat = p.stat()
                entries.append((p.name, int(stat.st_mtime * 1000), stat.st_size))
            except OSError:
                pass
        return tuple(entries)

    def _scan_telemetry_accounts(self) -> dict[str, dict[str, Any]]:
        accounts: dict[str, dict[str, Any]] = {}
        telemetry_dir = self.root_dir / "statsig"
        if not telemetry_dir.exists():
            return accounts
        for p in telemetry_dir.iterdir():
            data = safe_read_json(p)
            if not isinstance(data, dict):
                continue
            user = data.get("user") or {}
            custom = user.get("custom") or {}
            account_key = custom.get("account_uuid") or user.get("userID") or ""
            email = user.get("email") or custom.get("email") or ""
            if account_key:
                accounts[account_key] = {
                    "account_key": account_key,
                    "account_label": email or account_key[:8],
                }
        return accounts

    def _make_bucket_row(
        self,
        ts: float,
        usage: dict[str, Any],
        session_id: str,
        project_path: str | None,
        account_key: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        totals = numeric_usage_totals(usage)
        row: dict[str, Any] = {
            "ts": ts,
            "session_id": session_id,
            "project_path": project_path,
            "account_key": account_key,
            "model": model,
        }
        row.update(totals)
        return row

    def _parse_session_file(self, path: Path) -> SessionParseResult:
        session_id = path.stem
        project_dir = path.parent
        project_path_str: str | None = None

        claude_json = project_dir / "claude.json"
        if claude_json.exists():
            meta = safe_read_json(claude_json)
            if isinstance(meta, dict):
                project_path_str = meta.get("path") or meta.get("projectPath")

        if not project_path_str:
            name = project_dir.name
            if name.startswith("-"):
                project_path_str = name.replace("-", "/", 1)
                if not project_path_str.startswith("/"):
                    project_path_str = "/" + project_path_str

        lines: list[dict[str, Any]] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    lines.append(json.loads(raw_line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            file_mtime = None

        token_totals: dict[str, int] = {f: 0 for f in USAGE_FIELDS}
        usage_events: list[dict[str, Any]] = []
        limit_events: list[dict[str, Any]] = []
        models: list[str] = []
        first_ts: float | None = None
        last_ts: float | None = None
        last_prompt: str | None = None
        last_error: str | None = None
        account_key: str | None = None
        account_label: str | None = None
        state = "idle"
        user_prompts = 0
        assistant_messages = 0
        tool_uses = 0
        working_path: str | None = None

        for entry in lines:
            entry_type = entry.get("type")
            ts = parse_iso_ts(entry.get("timestamp"))
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            if entry_type == "user":
                msg = entry.get("message") or {}
                content = msg.get("content")
                if is_tool_result_only(content):
                    tool_uses += 1
                else:
                    user_prompts += 1
                    text = extract_user_text(content)
                    if text:
                        last_prompt = text[:300]
                err = extract_error_text(entry)
                if err:
                    last_error = err

            elif entry_type == "assistant":
                assistant_messages += 1
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model") or ""
                if model and model not in models:
                    models.append(model)
                ak = entry.get("account_uuid") or msg.get("account_uuid")
                if ak and not account_key:
                    account_key = ak
                    account_label = ak[:8]
                if usage:
                    merge_counter(token_totals, numeric_usage_totals(usage))
                    if ts:
                        usage_events.append(self._make_bucket_row(
                            ts, usage, session_id, project_path_str, account_key, model or None
                        ))

            elif entry_type == "system":
                sys_data = entry.get("system") or entry
                cwd = sys_data.get("cwd") or entry.get("cwd")
                if cwd and not working_path:
                    working_path = cwd
                ak = sys_data.get("account_uuid") or entry.get("account_uuid")
                if ak and not account_key:
                    account_key = ak
                    account_label = ak[:8]

            elif entry_type == "limit":
                state = "rate_limited"
                if ts:
                    limit_events.append({
                        "ts": ts,
                        "session_id": session_id,
                        "provider": "claude",
                        "raw": entry,
                    })

        last_access_at = last_ts
        if last_ts and (datetime.now(tz=LOCAL_TZ).timestamp() - last_ts) < 300:
            if user_prompts > assistant_messages:
                state = "awaiting_assistant"
            elif state != "rate_limited":
                state = "running"

        duration = 0
        if first_ts and last_ts:
            duration = int(last_ts - first_ts)

        return SessionParseResult(
            session_id=session_id,
            project_path=project_path_str,
            working_path=working_path or project_path_str,
            account_key=account_key,
            account_label=account_label,
            tokens={
                **token_totals,
                **numeric_usage_totals(token_totals),
            },
            state=state,
            first_event_at=first_ts,
            last_access_at=last_access_at,
            last_event_at=last_ts,
            last_prompt=last_prompt,
            last_error_text=last_error,
            resume_command=resume_command_for(session_id, project_path_str),
            open_command=open_command_for(project_path_str),
            duration_seconds=duration,
            models=models,
            usage_events=usage_events,
            limit_events=limit_events,
            user_prompts=user_prompts,
            assistant_messages=assistant_messages,
            tool_uses=tool_uses,
            file_mtime=file_mtime,
            session_file=str(path),
        )

    def _session_result(self, path: Path) -> SessionParseResult:
        key = str(path)
        sig = self._session_signature(path)
        cached = self._cache.get(key)
        if cached and cached.get("sig") == sig:
            return cached["result"]
        result = self._parse_session_file(path)
        self._cache[key] = {"sig": sig, "result": result}
        return result

    def _stats_cache_summary(self) -> dict[str, Any] | None:
        stats_path = self.root_dir / "statsig" / "cache.json"
        if not stats_path.exists():
            return None
        data = safe_read_json(stats_path)
        return data if isinstance(data, dict) else None

    def snapshot(self) -> dict[str, Any]:
        import time
        generated_at = time.time()
        sessions: list[dict[str, Any]] = []
        all_usage_events: list[dict[str, Any]] = []
        all_limit_events: list[dict[str, Any]] = []
        errors: list[str] = []

        accounts = self._scan_telemetry_accounts()

        if not self.projects_dir.exists():
            return {
                "generated_at": generated_at,
                "sessions": [],
                "usage_events": [],
                "limit_events": [],
                "accounts": list(accounts.values()),
                "errors": errors,
                "sources": {"root_dir": str(self.root_dir)},
            }

        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for session_file in project_dir.glob("*.jsonl"):
                try:
                    result = self._session_result(session_file)
                except Exception as exc:
                    errors.append(f"{session_file}: {compact_error(exc)}")
                    continue

                ak = result.account_key
                if ak and ak not in accounts:
                    accounts[ak] = {
                        "account_key": ak,
                        "account_label": result.account_label or ak[:8],
                    }

                tokens = result.tokens
                sessions.append({
                    "session_id": result.session_id,
                    "project_path": result.project_path,
                    "project_key": project_dir.name,
                    "working_path": result.working_path,
                    "account_key": ak,
                    "account_label": result.account_label,
                    "state": result.state,
                    "first_event_at": result.first_event_at,
                    "last_event_at": result.last_event_at,
                    "last_access_at": result.last_access_at,
                    "last_prompt": result.last_prompt,
                    "last_error_text": result.last_error_text,
                    "resume_command": result.resume_command,
                    "open_command": result.open_command,
                    "duration_seconds": result.duration_seconds,
                    "models": result.models,
                    "tokens": tokens,
                    "user_prompts": result.user_prompts,
                    "assistant_messages": result.assistant_messages,
                    "tool_uses": result.tool_uses,
                    "file_mtime": result.file_mtime,
                    "session_file": result.session_file,
                })
                all_usage_events.extend(result.usage_events)
                all_limit_events.extend(result.limit_events)

        sessions.sort(key=lambda s: s.get("last_access_at") or 0, reverse=True)

        return {
            "generated_at": generated_at,
            "sessions": sessions,
            "usage_events": all_usage_events,
            "limit_events": all_limit_events,
            "accounts": list(accounts.values()),
            "errors": errors,
            "sources": {"root_dir": str(self.root_dir)},
        }
