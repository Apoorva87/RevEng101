"""Rate-limit snapshot parsing helpers shared by the server and proxy."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)

RATE_LIMIT_WINDOW_ALIASES = {
    "5h": ("5h", "5-hour", "5hour", "5hr"),
    "5d": ("5d", "5-day", "5day"),
    "7d": ("7d", "7-day", "7day"),
}


def _normalize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Lower-case response headers for case-insensitive parsing."""
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if key is None or value is None:
            continue
        normalized[str(key).lower()] = str(value)
    return normalized


def _parse_fractional_value(
    value: str,
    *,
    allow_overage: bool = False,
) -> Optional[float]:
    """Parse a utilization-like value and normalize common percentage formats."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    if numeric <= 1:
        return numeric
    # Anthropic's unified utilization headers report fractional usage and can
    # legitimately exceed 1.0 when an account is over quota, e.g. "1.01".
    if allow_overage and numeric < 10:
        return numeric
    if numeric <= 100:
        return numeric / 100.0
    return None


def _parse_number(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_label_for_seconds(limit_window_seconds: Any) -> Optional[str]:
    try:
        seconds = int(limit_window_seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _iso_from_epoch_seconds(value: Any) -> Optional[str]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1_000_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def openai_usage_snapshot(body: Any) -> Optional[dict[str, Any]]:
    """Normalize ChatGPT usage JSON into the token rate-limit snapshot shape."""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None

    allowed = bool(rate_limit.get("allowed"))
    limit_reached = bool(rate_limit.get("limit_reached"))
    snapshot: dict[str, Any] = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "windows": [],
        "overall_status": "ok" if allowed and not limit_reached else "limited",
        "allowed": allowed,
        "limit_reached": limit_reached,
    }

    plan_type = body.get("plan_type")
    if isinstance(plan_type, str) and plan_type:
        snapshot["plan_type"] = plan_type

    for _, window_data in rate_limit.items():
        if not isinstance(window_data, dict):
            continue
        label = _window_label_for_seconds(window_data.get("limit_window_seconds"))
        if not label:
            continue

        used_percent = _parse_fractional_value(str(window_data.get("used_percent", "")))
        reset_iso = _iso_from_epoch_seconds(window_data.get("reset_at"))
        window: dict[str, Any] = {
            "label": label,
            "status": "ok" if allowed and not limit_reached else "limited",
        }
        if used_percent is not None:
            window["utilization"] = used_percent
            snapshot[f"{label}_utilization"] = used_percent
        snapshot[f"{label}_status"] = window["status"]
        if reset_iso:
            window["reset"] = reset_iso
            snapshot[f"{label}_reset"] = reset_iso
        snapshot["windows"].append(window)

    return snapshot if snapshot["windows"] else None


def openai_usage_ok(body: Any) -> Optional[bool]:
    """Interpret ChatGPT usage JSON as a health-check result."""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    spend_control = body.get("spend_control")
    spend_reached = (
        isinstance(spend_control, dict) and bool(spend_control.get("reached"))
    )
    return (
        bool(rate_limit.get("allowed"))
        and not bool(rate_limit.get("limit_reached"))
        and not spend_reached
    )


def rate_limit_snapshot_from_headers(
    headers: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Parse known/provider-specific rate-limit headers into a UI snapshot."""
    normalized = _normalize_headers(headers)
    if not normalized:
        return None

    snapshot: dict[str, Any] = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "windows": [],
    }
    windows_by_label: dict[str, dict[str, Any]] = {}

    def ensure_window(label: str) -> dict[str, Any]:
        window = windows_by_label.get(label)
        if window is None:
            window = {"label": label}
            windows_by_label[label] = window
        return window

    def first_value(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            value = normalized.get(candidate)
            if value not in (None, ""):
                return value
        return None

    for label in ("5h", "7d"):
        util_raw = normalized.get(
            f"anthropic-ratelimit-unified-{label}-utilization"
        )
        status_raw = normalized.get(f"anthropic-ratelimit-unified-{label}-status")
        reset_raw = normalized.get(f"anthropic-ratelimit-unified-{label}-reset")
        if util_raw is None and status_raw is None and reset_raw is None:
            continue

        window = ensure_window(label)
        util = (
            _parse_fractional_value(util_raw, allow_overage=True)
            if util_raw is not None
            else None
        )
        if util is not None:
            window["utilization"] = util
            snapshot[f"{label}_utilization"] = util
        elif util_raw is not None:
            logger.warning(
                "Ignoring invalid %s utilization header: %r",
                label,
                util_raw,
            )
        if status_raw is not None:
            window["status"] = status_raw
            snapshot[f"{label}_status"] = status_raw
        if reset_raw:
            window["reset"] = reset_raw
            snapshot[f"{label}_reset"] = reset_raw

    overall_status = first_value(
        "anthropic-ratelimit-unified-status",
        "x-ratelimit-status",
    )
    if overall_status:
        snapshot["overall_status"] = overall_status

    generic_metrics: dict[str, dict[str, Any]] = {}
    for header_name, raw_value in normalized.items():
        if "ratelimit" not in header_name:
            continue

        matched_label = None
        for label, aliases in RATE_LIMIT_WINDOW_ALIASES.items():
            if any(alias in header_name for alias in aliases):
                matched_label = label
                break
        if matched_label is None:
            continue

        metric = generic_metrics.setdefault(matched_label, {})
        if "utilization" in header_name or "usage" in header_name:
            util = _parse_fractional_value(raw_value)
            if util is not None:
                metric["utilization"] = util
        elif "remaining" in header_name:
            remaining = _parse_number(raw_value)
            if remaining is not None:
                metric["remaining"] = remaining
        elif "limit" in header_name:
            limit = _parse_number(raw_value)
            if limit is not None:
                metric["limit"] = limit
        elif "status" in header_name:
            metric["status"] = raw_value
        elif "reset" in header_name:
            metric["reset"] = raw_value

    for label, metric in generic_metrics.items():
        window = ensure_window(label)
        if "utilization" not in window:
            util = metric.get("utilization")
            if util is None:
                remaining = metric.get("remaining")
                limit = metric.get("limit")
                if remaining is not None and limit not in (None, 0):
                    util = 1.0 - max(0.0, min(remaining / limit, 1.0))
            if util is not None:
                window["utilization"] = util
                snapshot.setdefault(f"{label}_utilization", util)
        if "status" not in window and metric.get("status"):
            window["status"] = metric["status"]
            snapshot.setdefault(f"{label}_status", metric["status"])
        if "reset" not in window and metric.get("reset"):
            window["reset"] = metric["reset"]
            snapshot.setdefault(f"{label}_reset", metric["reset"])

    for label in ("5h", "5d", "7d"):
        window = windows_by_label.get(label)
        if window and any(key in window for key in ("utilization", "status", "reset")):
            snapshot["windows"].append(window)

    if not snapshot["windows"] and not snapshot.get("overall_status"):
        return None
    return snapshot


def update_token_rate_limits_from_headers(
    snapshots: MutableMapping[str, dict[str, Any]],
    token_id: str,
    headers: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Store a header-derived rate-limit snapshot for a token."""
    snapshot = rate_limit_snapshot_from_headers(headers)
    if snapshot:
        snapshots[token_id] = snapshot
    return snapshot


def update_token_rate_limits_from_probe(
    snapshots: MutableMapping[str, dict[str, Any]],
    token_id: str,
    provider_name: str,
    headers: Mapping[str, Any],
    body: Any,
) -> Optional[dict[str, Any]]:
    """Store the freshest rate-limit snapshot from either headers or probe body."""
    snapshot = rate_limit_snapshot_from_headers(headers)
    body_snapshot = openai_usage_snapshot(body) if provider_name == "openai" else None
    final_snapshot = body_snapshot or snapshot
    if final_snapshot:
        snapshots[token_id] = final_snapshot
    return final_snapshot
