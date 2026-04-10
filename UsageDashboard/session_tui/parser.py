"""JSONL and session index parsing for Claude Code session data."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    ChartMarker,
    DYNAMIC_TYPES,
    ParsedSession,
    SessionIndex,
    SessionRecord,
    UsageData,
)

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


# ---------------------------------------------------------------------------
# Utility functions (adapted from claude_sessions_dashboard.py)
# ---------------------------------------------------------------------------


def parse_iso_ts(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def format_relative(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    delta = datetime.now(tz=LOCAL_TZ) - datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def format_ts_short(ts: Optional[float]) -> str:
    """Format a timestamp as HH:MM:SS local time."""
    if ts is None:
        return "--:--:--"
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    return dt.strftime("%H:%M:%S")


def format_ts_local(ts: Optional[float]) -> str:
    """Format a timestamp in local time for labels."""
    if ts is None:
        return "--"
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    return dt.strftime("%m-%d %H:%M")


def extract_user_text(content: Any) -> Optional[str]:
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


def extract_assistant_text(content: Any) -> Optional[str]:
    """Extract displayable text from assistant message content."""
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "text" and item.get("text"):
            parts.append(str(item["text"]).strip())
        elif item_type == "tool_use":
            name = item.get("name", "?")
            parts.append(f"<tool_use: {name}>")
        elif item_type == "thinking":
            parts.append("<thinking>")
    merged = " ".join(p for p in parts if p)
    return merged or None


def extract_usage(
    message: dict,
    *,
    timestamp: Optional[float] = None,
    record_uuid: Optional[str] = None,
    request_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    is_sidechain: bool = False,
    source: str = "main",
) -> Optional[UsageData]:
    """Extract UsageData from an assistant message dict."""
    usage = message.get("usage")
    if not usage or not isinstance(usage, dict):
        return None
    cache_creation = usage.get("cache_creation") or {}
    server_tool_use = usage.get("server_tool_use") or {}
    return UsageData(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        ephemeral_5m_input_tokens=int(cache_creation.get("ephemeral_5m_input_tokens") or 0),
        ephemeral_1h_input_tokens=int(cache_creation.get("ephemeral_1h_input_tokens") or 0),
        web_search_requests=int(server_tool_use.get("web_search_requests") or 0),
        web_fetch_requests=int(server_tool_use.get("web_fetch_requests") or 0),
        model=str(message.get("model") or "unknown"),
        service_tier=usage.get("service_tier"),
        speed=usage.get("speed"),
        timestamp=timestamp,
        record_uuid=record_uuid,
        request_id=request_id,
        agent_id=agent_id,
        is_sidechain=is_sidechain,
        source=source,
    )


def truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_text_items(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            texts.append(stripped)
        return texts
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    texts.append(stripped)
            elif isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    texts.append(str(item["text"]).strip())
                elif item.get("type") == "tool_result":
                    texts.extend(_extract_text_items(item.get("content")))
    return texts


def _extract_rate_limit_texts(record: SessionRecord) -> list[str]:
    texts: list[str] = []
    raw = record.raw

    message = raw.get("message") or {}
    texts.extend(_extract_text_items(message.get("content")))

    tool_use_result = raw.get("toolUseResult")
    if isinstance(tool_use_result, dict):
        texts.extend(_extract_text_items(tool_use_result.get("content")))
        stdout = tool_use_result.get("stdout")
        if isinstance(stdout, str) and "anthropic-ratelimit" in stdout:
            texts.append(stdout.strip())
    elif isinstance(tool_use_result, str):
        texts.append(tool_use_result.strip())

    return [text for text in texts if text]


def _parse_reset_timestamp(text: str, event_ts: Optional[float]) -> Optional[float]:
    if event_ts is None:
        return None

    match = re.search(r"resets\s+(.+?)\s+\(([^)]+)\)", text)
    if not match:
        return None

    reset_text = match.group(1).strip()
    event_dt = datetime.fromtimestamp(event_ts, tz=LOCAL_TZ)

    def parse_clock(value: str) -> Optional[datetime]:
        for fmt in ("%I%p", "%I:%M%p"):
            try:
                parsed = datetime.strptime(value.upper(), fmt)
                return event_dt.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=0,
                    microsecond=0,
                )
            except ValueError:
                continue
        return None

    def parse_month_day(value: str) -> Optional[datetime]:
        for fmt in ("%b %d at %I%p", "%b %d at %I:%M%p"):
            try:
                parsed = datetime.strptime(value, fmt)
                return datetime(
                    year=event_dt.year,
                    month=parsed.month,
                    day=parsed.day,
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=0,
                    microsecond=0,
                    tzinfo=LOCAL_TZ,
                )
            except ValueError:
                continue
        return None

    parsed_dt = parse_month_day(reset_text) or parse_clock(reset_text)
    if parsed_dt is None:
        return None

    if parsed_dt <= event_dt:
        if re.match(r"^[0-9]{1,2}(:[0-9]{2})?[ap]m$", reset_text.lower()):
            parsed_dt = parsed_dt + timedelta(days=1)
        elif parsed_dt.month < event_dt.month or (
            parsed_dt.month == event_dt.month and parsed_dt.day < event_dt.day
        ):
            parsed_dt = parsed_dt.replace(year=parsed_dt.year + 1)

    return parsed_dt.timestamp()


# ---------------------------------------------------------------------------
# Session index loading
# ---------------------------------------------------------------------------


def load_session_indexes(root: Path) -> list[SessionIndex]:
    """Read all session index files from ~/.claude/sessions/*.json."""
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return []

    indexes: list[SessionIndex] = []
    for json_file in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("sessionId", "")
        if not session_id:
            continue

        started_ms = data.get("startedAt", 0)
        idx = SessionIndex(
            pid=int(data.get("pid", 0)),
            session_id=session_id,
            cwd=data.get("cwd", ""),
            started_at=started_ms / 1000.0 if started_ms > 1e12 else float(started_ms),
            kind=data.get("kind", "unknown"),
            entrypoint=data.get("entrypoint", "unknown"),
        )

        # Try to find the JSONL and extract slug
        jsonl = find_jsonl_for_session(root, session_id)
        if jsonl:
            idx.jsonl_path = str(jsonl)
            idx.slug = _extract_slug(jsonl)

        indexes.append(idx)

    # Sort by started_at descending (newest first)
    indexes.sort(key=lambda s: s.started_at, reverse=True)
    return indexes


def find_jsonl_for_session(root: Path, session_id: str) -> Optional[Path]:
    """Find the JSONL file for a given session ID by globbing across all projects."""
    projects_dir = root / "projects"
    if not projects_dir.is_dir():
        return None

    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _extract_slug(jsonl_path: Path) -> Optional[str]:
    """Read the last few lines of a JSONL to find the slug field."""
    try:
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
        # Scan from the end — slug appears on assistant/user messages
        for line in reversed(raw.splitlines()[-30:]):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                slug = entry.get("slug")
                if slug:
                    return str(slug)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Full JSONL parsing
# ---------------------------------------------------------------------------


def parse_jsonl(
    path: Path,
    truncate_at: int = 200,
    source: str = "main",
) -> list[SessionRecord]:
    """Parse a session JSONL file into a list of SessionRecords."""
    records: list[SessionRecord] = []

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records

    for raw_line in raw_text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        record_type = str(entry.get("type") or "")
        if not record_type:
            continue

        timestamp = parse_iso_ts(entry.get("timestamp"))
        uuid = entry.get("uuid")
        request_id = entry.get("requestId")
        agent_id = entry.get("agentId")
        is_sidechain = bool(entry.get("isSidechain", False))
        is_dynamic = record_type in DYNAMIC_TYPES
        text_preview = None
        usage = None

        message = entry.get("message") or {}
        content = message.get("content")

        if record_type == "user":
            text_preview = truncate(extract_user_text(content), truncate_at)
            # For tool results, show a summary
            if text_preview is None and isinstance(content, list):
                tool_results = [
                    item for item in content
                    if isinstance(item, dict) and item.get("type") == "tool_result"
                ]
                if tool_results:
                    text_preview = f"<tool_result x{len(tool_results)}>"

        elif record_type == "assistant":
            text_preview = truncate(extract_assistant_text(content), truncate_at)
            usage = extract_usage(
                message,
                timestamp=timestamp,
                record_uuid=uuid,
                request_id=request_id,
                agent_id=agent_id,
                is_sidechain=is_sidechain,
                source=source,
            )
            # Skip assistant records that are just streaming continuations with no new content
            # (they share the same requestId but have incremental content)

        elif record_type == "system":
            subtype = entry.get("subtype", "")
            if subtype == "turn_duration":
                text_preview = f"turn_duration: {entry.get('durationMs', 0)}ms"
            elif subtype == "api_error":
                cause = entry.get("cause") or entry.get("error") or {}
                code = cause.get("code", "") if isinstance(cause, dict) else str(cause)
                text_preview = f"api_error: {code} (retry {entry.get('retryAttempt', '?')})"
            elif subtype == "local_command":
                text_preview = truncate(str(entry.get("content", "")), truncate_at)
            elif subtype:
                text_preview = f"{subtype}"
            else:
                text_preview = truncate(str(entry.get("content", "")), truncate_at)

        elif record_type == "last-prompt":
            text_preview = truncate(str(entry.get("lastPrompt", "")), truncate_at)

        elif record_type == "permission-mode":
            text_preview = f"mode: {entry.get('permissionMode', '?')}"

        elif record_type == "file-history-snapshot":
            snap = entry.get("snapshot") or {}
            backups = snap.get("trackedFileBackups") or {}
            text_preview = f"files tracked: {len(backups)}" if backups else "snapshot"

        elif record_type == "attachment":
            attachment = entry.get("attachment") or {}
            att_type = attachment.get("type", "")
            text_preview = f"attachment: {att_type}"

        elif record_type == "queue-operation":
            op = entry.get("operation", "?")
            text_preview = f"queue: {op}"

        records.append(SessionRecord(
            type=record_type,
            timestamp=timestamp,
            uuid=uuid,
            text_preview=text_preview,
            usage=usage,
            is_dynamic=is_dynamic,
            raw=entry,
        ))

    return records


def _extract_agent_launch_markers(records: list[SessionRecord]) -> list[ChartMarker]:
    markers: list[ChartMarker] = []

    for record in records:
        if record.type != "assistant":
            continue

        content = (record.raw.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_use" or item.get("name") != "Agent":
                continue

            input_data = item.get("input") or {}
            label_parts = ["Agent launch"]
            subagent_type = input_data.get("subagent_type")
            description = input_data.get("description")
            if subagent_type:
                label_parts.append(str(subagent_type))
            elif description:
                label_parts.append(str(description))

            markers.append(
                ChartMarker(
                    kind="agent_launch",
                    timestamp=record.timestamp,
                    label=": ".join(label_parts),
                )
            )

    return markers


def _load_subagent_markers(main_jsonl_path: Path, truncate_at: int = 200) -> list[ChartMarker]:
    markers: list[ChartMarker] = []
    subagents_dir = main_jsonl_path.parent / main_jsonl_path.stem / "subagents"
    if not subagents_dir.is_dir():
        return markers

    for sub_file in sorted(subagents_dir.glob("*.jsonl")):
        records = parse_jsonl(sub_file, truncate_at=truncate_at, source="subagent")
        if not records:
            continue

        timestamps = [record.timestamp for record in records if record.timestamp is not None]
        if not timestamps:
            continue

        start_ts = min(timestamps)
        end_ts = max(timestamps)
        total_tokens = sum(
            record.usage.total_tokens
            for record in records
            if record.usage is not None
        )

        agent_id = None
        for record in records:
            raw_agent_id = record.raw.get("agentId")
            if raw_agent_id:
                agent_id = str(raw_agent_id)
                break

        if not agent_id and sub_file.stem.startswith("agent-"):
            agent_id = sub_file.stem[len("agent-") :]

        short_agent = (agent_id or sub_file.stem)[:12]
        label = f"Subagent {short_agent}"

        markers.append(
            ChartMarker(
                kind="subagent_start",
                timestamp=start_ts,
                label=label,
                agent_id=agent_id,
                total_tokens=total_tokens,
            )
        )
        if end_ts > start_ts:
            markers.append(
                ChartMarker(
                    kind="subagent_end",
                    timestamp=end_ts,
                    label=label,
                    agent_id=agent_id,
                    total_tokens=total_tokens,
                )
            )

    return markers


def _extract_rate_limit_markers(records: list[SessionRecord]) -> list[ChartMarker]:
    markers: list[ChartMarker] = []
    seen_hits: set[tuple[int, str]] = set()
    seen_resets: set[int] = set()

    for record in records:
        texts = _extract_rate_limit_texts(record)
        if not texts:
            continue

        for text in texts:
            lowered = text.lower()
            is_limit_text = (
                "you've hit your limit" in lowered
                or "rate limit reached" in lowered
                or 'error":"rate_limit"' in lowered
            )
            if not is_limit_text:
                continue

            event_ts = record.timestamp
            label = "Rate limit hit"
            if "you've hit your limit" in lowered:
                label = "You've hit your limit"
            elif "rate limit reached" in lowered:
                label = "Rate limit reached"

            hit_key = (
                int(round((event_ts or 0.0) * 10)),
                label,
            )
            if hit_key not in seen_hits:
                markers.append(
                    ChartMarker(
                        kind="rate_limit_hit",
                        timestamp=event_ts,
                        label=label,
                        detail=text,
                    )
                )
                seen_hits.add(hit_key)

            reset_at = _parse_reset_timestamp(text, event_ts)
            if reset_at is None:
                continue

            reset_key = int(round(reset_at))
            if reset_key in seen_resets:
                continue
            seen_resets.add(reset_key)
            markers.append(
                ChartMarker(
                    kind="rate_limit_reset",
                    timestamp=reset_at,
                    label=f"Limit reset at {format_ts_local(reset_at)}",
                    detail=text,
                    reset_at=reset_at,
                )
            )

    return markers


def build_parsed_session(index: SessionIndex, truncate_at: int = 200) -> ParsedSession:
    """Load and parse a full session from its index entry."""
    if index.jsonl_path:
        jsonl_path = Path(index.jsonl_path)
        records = parse_jsonl(jsonl_path, truncate_at=truncate_at, source="main")
        chart_markers = _extract_agent_launch_markers(records)
        chart_markers.extend(_extract_rate_limit_markers(records))
        chart_markers.extend(_load_subagent_markers(jsonl_path, truncate_at=truncate_at))
        chart_markers.sort(key=lambda marker: (marker.timestamp is None, marker.timestamp or 0.0, marker.kind))
    else:
        records = []
        chart_markers = []
    return ParsedSession(index=index, records=records, chart_markers=chart_markers)
