"""Shared token usage timeline aggregation for unified dashboard views."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

SERIES_META: dict[str, dict[str, Any]] = {
    "total_tokens": {"label": "Total", "color": "#f6c177", "default": True},
    "input_tokens": {"label": "Input", "color": "#62a7ff", "default": True},
    "cached_tokens": {"label": "Cached", "color": "#37c7a7", "default": True},
    "output_tokens": {"label": "Output", "color": "#ff7eb3", "default": True},
    "reasoning_output_tokens": {"label": "Reasoning", "color": "#a78bfa", "default": False},
    "cache_read_input_tokens": {"label": "Cache Read", "color": "#67e8f9", "default": False},
    "cache_creation_input_tokens": {"label": "Cache Create", "color": "#3ddc84", "default": False},
}

PRESET_WINDOWS = {
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "180d": timedelta(days=180),
    "365d": timedelta(days=365),
}

BUCKET_CHOICES = {"5m", "15m", "hour", "day", "week", "month"}
LOW_WATERMARK_PERCENT = 5.0
HIGH_WATERMARK_PERCENT = 100.0


def _now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def _empty_series() -> dict[str, int]:
    return {
        "total_tokens": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _empty_bucket_row() -> dict[str, Any]:
    return {
        "series": _empty_series(),
        "projects": {},
        "sessions": {},
        "markers": [],
        "marker_counts": {},
        "limit_summary": _empty_limit_summary(),
    }


def _empty_limit_summary() -> dict[str, Any]:
    return {
        "five_hour_max_used_percent": None,
        "seven_day_max_used_percent": None,
        "claude_limit_hits": 0,
    }


def _merge_series(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = int(target.get(key) or 0) + int(value or 0)


def _normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    usage = dict(raw or {})
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or usage.get("cached_input_tokens") or 0)
    if not cached_tokens:
        cached_tokens = cache_read + cache_create
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_output = int(usage.get("reasoning_output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if not total_tokens:
        total_tokens = input_tokens + cached_tokens + output_tokens + reasoning_output
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
    }


def _event_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_window_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.timestamp()


def _bucket_start(ts: float, bucket: str) -> datetime:
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    if bucket == "5m":
        return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
    if bucket == "15m":
        return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start - timedelta(days=start.weekday())
    if bucket == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported bucket: {bucket}")


def _bucket_end(start: datetime, bucket: str) -> datetime:
    if bucket == "5m":
        return start + timedelta(minutes=5)
    if bucket == "15m":
        return start + timedelta(minutes=15)
    if bucket == "hour":
        return start + timedelta(hours=1)
    if bucket == "day":
        return start + timedelta(days=1)
    if bucket == "week":
        return start + timedelta(days=7)
    if bucket == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    raise ValueError(f"Unsupported bucket: {bucket}")


def _bucket_range(start_ts: float, end_ts: float, bucket: str) -> list[datetime]:
    start_dt = _bucket_start(start_ts, bucket)
    end_dt = _bucket_start(end_ts, bucket)
    current = start_dt
    values: list[datetime] = []
    while current <= end_dt:
        values.append(current)
        current = _bucket_end(current, bucket)
    return values


def _bucket_label(start: datetime, bucket: str) -> str:
    if bucket in {"5m", "15m", "hour"}:
        return start.strftime("%m-%d %H:%M")
    if bucket == "day":
        return start.strftime("%Y-%m-%d")
    if bucket == "week":
        return f"Week of {start.strftime('%Y-%m-%d')}"
    if bucket == "month":
        return start.strftime("%Y-%m")
    return start.isoformat()


def _series_keys_for(provider_filter: str) -> list[str]:
    if provider_filter == "claude":
        return [
            "total_tokens",
            "input_tokens",
            "cached_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        ]
    if provider_filter == "codex":
        return [
            "total_tokens",
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ]
    return [
        "total_tokens",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ]


def _series_options_for(provider_filter: str) -> list[dict[str, Any]]:
    return [
        {"key": key, **SERIES_META[key]}
        for key in _series_keys_for(provider_filter)
    ]


def _percent_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_label(minutes: Any) -> str | None:
    if minutes is None:
        return None
    try:
        total_minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    if total_minutes <= 0:
        return None
    if total_minutes % 1440 == 0:
        return f"{total_minutes // 1440}d"
    if total_minutes % 60 == 0:
        return f"{total_minutes // 60}h"
    return f"{total_minutes}m"


def _limit_state(percent: float | None) -> str | None:
    if percent is None:
        return None
    if percent >= HIGH_WATERMARK_PERCENT:
        return "exhausted"
    if percent <= LOW_WATERMARK_PERCENT:
        return "fresh"
    return "active"


def _resolve_window(
    preset: str,
    start_raw: str | None,
    end_raw: str | None,
    events: list[dict[str, Any]],
    limit_events: list[dict[str, Any]],
) -> tuple[float, float, str]:
    preset = (preset or "7d").strip().lower()
    now_ts = time.time()
    all_ts = [
        *[event["timestamp"] for event in events if event.get("timestamp") is not None],
        *[event["timestamp"] for event in limit_events if event.get("timestamp") is not None],
    ]

    if preset == "custom":
        start_ts = _parse_window_time(start_raw)
        end_ts = _parse_window_time(end_raw)
        if start_ts is None or end_ts is None or end_ts <= start_ts:
            raise ValueError("custom preset requires valid start and end timestamps")
        return start_ts, end_ts, "custom"

    if preset == "all":
        min_ts = min(all_ts, default=now_ts - 30 * 86400)
        max_ts = max(all_ts, default=now_ts)
        return min_ts, max_ts, "all"

    delta = PRESET_WINDOWS.get(preset)
    if delta is None:
        raise ValueError(f"unknown preset: {preset}")
    return now_ts - delta.total_seconds(), now_ts, preset


def _normalize_usage_event(provider_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    ts = _event_timestamp(raw.get("timestamp"))
    if ts is None:
        return None
    project_name = str(raw.get("project_name") or raw.get("project") or "(unknown)")
    project_path = str(raw.get("project_path") or raw.get("cwd") or project_name)
    return {
        "provider": provider_id,
        "timestamp": ts,
        "session_id": str(raw.get("session_id") or ""),
        "session_label": str(raw.get("session_label") or raw.get("display_label") or raw.get("session_id") or ""),
        "project_name": project_name,
        "project_path": project_path,
        "account_key": str(raw.get("account_key") or "unknown"),
        "account_label": str(raw.get("account_label") or "Unknown"),
        "usage": _normalize_usage(raw.get("usage") or {}),
    }


def _normalize_limit_event(provider_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    ts = _event_timestamp(raw.get("timestamp"))
    if ts is None:
        return None
    project_name = str(raw.get("project_name") or raw.get("project") or "(unknown)")
    project_path = str(raw.get("project_path") or raw.get("cwd") or project_name)
    normalized = {
        "provider": provider_id,
        "timestamp": ts,
        "session_id": str(raw.get("session_id") or ""),
        "session_label": str(raw.get("session_label") or raw.get("display_label") or raw.get("session_id") or ""),
        "project_name": project_name,
        "project_path": project_path,
        "account_key": str(raw.get("account_key") or "unknown"),
        "account_label": str(raw.get("account_label") or "Unknown"),
        "kind": str(raw.get("kind") or ""),
        "label": str(raw.get("label") or "").strip() or None,
        "plan_type": raw.get("plan_type"),
        "primary_used_percent": raw.get("primary_used_percent"),
        "primary_window_minutes": raw.get("primary_window_minutes"),
        "primary_resets_at": raw.get("primary_resets_at"),
        "secondary_used_percent": raw.get("secondary_used_percent"),
        "secondary_window_minutes": raw.get("secondary_window_minutes"),
        "secondary_resets_at": raw.get("secondary_resets_at"),
    }
    return normalized


def _collect_provider_events(
    provider_snapshots: list[dict[str, Any]],
    provider_filter: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    usage_events: list[dict[str, Any]] = []
    limit_events: list[dict[str, Any]] = []
    providers: list[dict[str, str]] = []

    for snapshot in provider_snapshots:
        provider_id = str(snapshot.get("provider") or "unknown")
        providers.append({"id": provider_id, "label": provider_id.title()})
        if provider_filter != "all" and provider_id != provider_filter:
            continue
        for raw in snapshot.get("usage_events") or []:
            normalized = _normalize_usage_event(provider_id, raw)
            if normalized is not None:
                usage_events.append(normalized)
        for raw in snapshot.get("limit_events") or []:
            normalized = _normalize_limit_event(provider_id, raw)
            if normalized is not None:
                limit_events.append(normalized)

    usage_events.sort(key=lambda item: item["timestamp"])
    limit_events.sort(key=lambda item: item["timestamp"])
    return usage_events, limit_events, providers


def _update_limit_summary(summary: dict[str, Any], event: dict[str, Any]) -> None:
    for window_label, used_percent in (
        (_window_label(event.get("primary_window_minutes")), _percent_value(event.get("primary_used_percent"))),
        (_window_label(event.get("secondary_window_minutes")), _percent_value(event.get("secondary_used_percent"))),
    ):
        if window_label not in {"5h", "7d"} or used_percent is None:
            continue
        key_name = "five_hour_max_used_percent" if window_label == "5h" else "seven_day_max_used_percent"
        current = summary[key_name]
        summary[key_name] = used_percent if current is None else max(current, used_percent)
    if event["provider"] == "claude":
        summary["claude_limit_hits"] += 1


def _aggregate_limit_window(
    limit_events: list[dict[str, Any]],
    start_ts: float,
    end_ts: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    marker_state: dict[tuple[str, str, str], str | None] = {}
    markers: list[dict[str, Any]] = []
    summary = _empty_limit_summary()

    for event in limit_events:
        if event["timestamp"] > end_ts:
            break
        derived_markers = _derived_markers(event, marker_state)
        if not (start_ts <= event["timestamp"] < end_ts):
            continue
        _update_limit_summary(summary, event)
        markers.extend(derived_markers)

    return markers, summary


def _derived_markers(
    limit_event: dict[str, Any],
    previous_states: dict[tuple[str, str, str], str | None],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    provider = limit_event["provider"]
    if provider == "claude":
        markers.append(
            {
                "provider": provider,
                "kind": "claude_rate_limit",
                "severity": "danger",
                "label": limit_event.get("label") or "Claude rate limit",
                "account_key": limit_event.get("account_key"),
                "account_label": limit_event.get("account_label"),
                "session_id": limit_event.get("session_id"),
                "session_label": limit_event.get("session_label"),
                "project_name": limit_event.get("project_name"),
            }
        )
        return markers

    account_key = str(limit_event.get("account_key") or "unknown")
    account_label = str(limit_event.get("account_label") or "Unknown")
    windows = [
        (
            _window_label(limit_event.get("primary_window_minutes")),
            _percent_value(limit_event.get("primary_used_percent")),
            limit_event.get("primary_resets_at"),
        ),
        (
            _window_label(limit_event.get("secondary_window_minutes")),
            _percent_value(limit_event.get("secondary_used_percent")),
            limit_event.get("secondary_resets_at"),
        ),
    ]
    seen_windows: set[str] = set()
    for window, used_percent, resets_at in windows:
        if not window or used_percent is None or window in seen_windows:
            continue
        seen_windows.add(window)
        current_state = _limit_state(used_percent)
        state_key = (provider, account_key, window)
        previous_state = previous_states.get(state_key)
        previous_states[state_key] = current_state
        if current_state not in {"fresh", "exhausted"} or current_state == previous_state:
            continue
        if current_state == "fresh":
            label = (
                f"Codex {window} reset ({used_percent:.0f}%)"
                if used_percent <= 0
                else f"Codex {window} under 5% ({used_percent:.0f}%)"
            )
            kind = "codex_limit_reset"
            severity = "good"
        else:
            label = f"Codex {window} exhausted ({used_percent:.0f}%)"
            kind = "codex_limit_exhausted"
            severity = "danger"
        markers.append(
            {
                "provider": provider,
                "kind": kind,
                "severity": severity,
                "label": label,
                "window": window,
                "used_percent": used_percent,
                "resets_at": resets_at,
                "account_key": account_key,
                "account_label": account_label,
                "session_id": limit_event.get("session_id"),
                "session_label": limit_event.get("session_label"),
                "project_name": limit_event.get("project_name"),
                "project_path": limit_event.get("project_path"),
            }
        )
    return markers


def build_usage_timeline(
    provider_snapshots: list[dict[str, Any]],
    provider_filter: str = "all",
    bucket: str = "hour",
    preset: str = "7d",
    start_raw: str | None = None,
    end_raw: str | None = None,
) -> dict[str, Any]:
    provider_filter = (provider_filter or "all").strip().lower()
    bucket = (bucket or "hour").strip().lower()
    if bucket not in BUCKET_CHOICES:
        raise ValueError(f"Unsupported bucket: {bucket}")

    usage_events, limit_events, providers = _collect_provider_events(provider_snapshots, provider_filter)

    start_ts, end_ts, resolved_preset = _resolve_window(preset, start_raw, end_raw, usage_events, limit_events)

    filtered_usage = [
        event for event in usage_events
        if start_ts <= event["timestamp"] <= end_ts
    ]
    filtered_limits = [
        event for event in limit_events
        if start_ts <= event["timestamp"] <= end_ts
    ]

    rows: dict[datetime, dict[str, Any]] = {
        bucket_dt: _empty_bucket_row()
        for bucket_dt in _bucket_range(start_ts, end_ts, bucket)
    }
    totals = _empty_series()

    for event in filtered_usage:
        _merge_series(totals, event["usage"])
        bucket_dt = _bucket_start(event["timestamp"], bucket)
        row = rows.setdefault(bucket_dt, _empty_bucket_row())
        _merge_series(row["series"], event["usage"])
        project = row["projects"].setdefault(
            event["project_name"],
            {"name": event["project_name"], "project_path": event["project_path"], "series": _empty_series()},
        )
        _merge_series(project["series"], event["usage"])
        session_key = f"{event['provider']}:{event['session_id']}"
        session = row["sessions"].setdefault(
            session_key,
            {
                "provider": event["provider"],
                "session_id": event["session_id"],
                "session_label": event["session_label"],
                "project_name": event["project_name"],
                "project_path": event["project_path"],
                "account_label": event["account_label"],
                "series": _empty_series(),
            },
        )
        _merge_series(session["series"], event["usage"])

    marker_state: dict[tuple[str, str, str], str | None] = {}
    for event in limit_events:
        if event["timestamp"] > end_ts:
            break
        within_window = start_ts <= event["timestamp"] <= end_ts
        derived_markers = _derived_markers(event, marker_state)
        if not within_window:
            continue
        bucket_dt = _bucket_start(event["timestamp"], bucket)
        row = rows.setdefault(bucket_dt, _empty_bucket_row())
        _update_limit_summary(row["limit_summary"], event)

        for marker in derived_markers:
            key = "::".join(
                [
                    str(marker["kind"]),
                    str(marker.get("window") or ""),
                    str(marker.get("account_key") or ""),
                    str(marker.get("label") or ""),
                ]
            )
            if key in row["marker_counts"]:
                row["marker_counts"][key] += 1
            else:
                row["marker_counts"][key] = 1
                row["markers"].append(marker)

    buckets_payload = []
    for bucket_dt in sorted(rows.keys()):
        row = rows[bucket_dt]
        project_rows = sorted(
            (
                {
                    "name": project["name"],
                    "project_path": project["project_path"],
                    "series": project["series"],
                }
                for project in row["projects"].values()
            ),
            key=lambda item: item["series"]["total_tokens"],
            reverse=True,
        )
        session_rows = sorted(
            row["sessions"].values(),
            key=lambda item: item["series"]["total_tokens"],
            reverse=True,
        )
        buckets_payload.append(
            {
                "bucket_start": bucket_dt.timestamp(),
                "bucket_end": _bucket_end(bucket_dt, bucket).timestamp(),
                "label": _bucket_label(bucket_dt, bucket),
                "series": row["series"],
                "project_count": len(project_rows),
                "session_count": len(session_rows),
                "top_projects": project_rows[:6],
                "top_sessions": session_rows[:8],
                "markers": row["markers"],
                "marker_count": len(row["markers"]),
                "limit_summary": row["limit_summary"],
            }
        )

    return {
        "generated_at": time.time(),
        "timezone": str(LOCAL_TZ),
        "provider": provider_filter,
        "providers": providers,
        "window": {
            "preset": resolved_preset,
            "bucket": bucket,
            "start": start_ts,
            "end": end_ts,
        },
        "series_options": _series_options_for(provider_filter),
        "totals": totals,
        "event_count": len(filtered_usage),
        "limit_event_count": len(filtered_limits),
        "bucket_count": len(buckets_payload),
        "buckets": buckets_payload,
    }


def build_usage_breakdown(
    provider_snapshots: list[dict[str, Any]],
    provider_filter: str = "all",
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    provider_filter = (provider_filter or "all").strip().lower()
    if start_ts is None or end_ts is None:
        raise ValueError("range breakdown requires start_ts and end_ts")
    start_ts = float(start_ts)
    end_ts = float(end_ts)
    if end_ts <= start_ts:
        raise ValueError("range breakdown end_ts must be greater than start_ts")

    usage_events, limit_events, providers = _collect_provider_events(provider_snapshots, provider_filter)
    filtered_usage = [
        event for event in usage_events
        if start_ts <= event["timestamp"] < end_ts
    ]
    totals = _empty_series()
    projects: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}

    for event in filtered_usage:
        _merge_series(totals, event["usage"])
        project_key = event["project_path"] or event["project_name"]
        project = projects.setdefault(
            project_key,
            {
                "name": event["project_name"],
                "project_path": event["project_path"],
                "series": _empty_series(),
            },
        )
        _merge_series(project["series"], event["usage"])

        session_key = f"{event['provider']}:{event['session_id']}"
        session = sessions.setdefault(
            session_key,
            {
                "provider": event["provider"],
                "session_id": event["session_id"],
                "session_label": event["session_label"],
                "project_name": event["project_name"],
                "project_path": event["project_path"],
                "account_label": event["account_label"],
                "series": _empty_series(),
            },
        )
        _merge_series(session["series"], event["usage"])

    markers, limit_summary = _aggregate_limit_window(limit_events, start_ts, end_ts)
    project_rows = sorted(
        projects.values(),
        key=lambda item: item["series"]["total_tokens"],
        reverse=True,
    )
    session_rows = sorted(
        sessions.values(),
        key=lambda item: item["series"]["total_tokens"],
        reverse=True,
    )

    return {
        "generated_at": time.time(),
        "timezone": str(LOCAL_TZ),
        "provider": provider_filter,
        "providers": providers,
        "range": {
            "start": start_ts,
            "end": end_ts,
        },
        "totals": totals,
        "event_count": len(filtered_usage),
        "project_count": len(project_rows),
        "session_count": len(session_rows),
        "marker_count": len(markers),
        "limit_summary": limit_summary,
        "markers": markers,
        "project_rows": project_rows,
        "session_rows": session_rows,
    }
