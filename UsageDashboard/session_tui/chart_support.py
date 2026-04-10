"""Shared chart-axis and marker helpers for session visualizations."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import ChartMarker, UsageData

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc


@dataclass
class AxisLayout:
    """Resolved x-axis coordinates and labels for a chart."""

    mode: str
    x_values: list[float]
    xlabel: str
    base_timestamp: float | None = None
    xticks: list[float] = field(default_factory=list)
    xlabels: list[str] = field(default_factory=list)


def format_elapsed_label(seconds: float) -> str:
    """Format an elapsed duration compactly for axis labels."""
    total_seconds = max(0, int(round(seconds)))
    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes, secs = divmod(total_seconds, 60)
    if total_seconds < 600 and secs:
        return f"{minutes}m{secs:02d}s"
    if minutes < 60:
        return f"{minutes}m"

    hours, rem_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if rem_minutes == 0 else f"{hours}h{rem_minutes:02d}m"

    days, rem_hours = divmod(hours, 24)
    return f"{days}d" if rem_hours == 0 else f"{days}d{rem_hours:02d}h"


def format_local_timestamp(ts: float | None) -> str:
    """Format a local timestamp for selected-range summaries."""
    if ts is None:
        return "--"
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    now = datetime.now(tz=LOCAL_TZ)
    fmt = "%H:%M:%S" if dt.date() == now.date() else "%m-%d %H:%M"
    return dt.strftime(fmt)


def build_axis_layout(usage_series: list[UsageData], axis_mode: str) -> AxisLayout:
    """Build message-count or elapsed-time coordinates for usage data."""
    if axis_mode == "timeline":
        known_timestamps = [u.timestamp for u in usage_series if u.timestamp is not None]
        if known_timestamps:
            base_ts = known_timestamps[0]
            x_values: list[float] = []
            last_x = 0.0
            for usage in usage_series:
                if usage.timestamp is None:
                    x_pos = last_x
                else:
                    x_pos = max(0.0, usage.timestamp - base_ts)
                    if x_pos < last_x:
                        x_pos = last_x
                x_values.append(x_pos)
                last_x = x_pos

            return AxisLayout(
                mode="timeline",
                x_values=x_values,
                xlabel="Elapsed Time",
                base_timestamp=base_ts,
            )

    return AxisLayout(
        mode="message",
        x_values=[float(i) for i in range(1, len(usage_series) + 1)],
        xlabel="Message #",
    )


def build_time_ticks(start_x: float, end_x: float, count: int = 6) -> tuple[list[float], list[str]]:
    """Build evenly spaced elapsed-time ticks for the x-axis."""
    start = max(0.0, start_x)
    end = max(start, end_x)
    if end == start:
        return [start], [format_elapsed_label(start)]

    tick_count = max(2, count)
    raw_positions = [
        start + ((end - start) * idx / (tick_count - 1))
        for idx in range(tick_count)
    ]

    positions: list[float] = []
    labels: list[str] = []
    for position in raw_positions:
        rounded = round(position, 3)
        if positions and abs(rounded - positions[-1]) < 1e-6:
            continue
        positions.append(rounded)
        labels.append(format_elapsed_label(position))

    return positions, labels


def marker_x_position(
    marker: ChartMarker,
    usage_series: list[UsageData],
    axis_layout: AxisLayout,
) -> float | None:
    """Map a chart marker onto the current x-axis."""
    if marker.timestamp is None or not usage_series:
        return None

    if axis_layout.mode == "timeline" and axis_layout.base_timestamp is not None:
        return max(0.0, marker.timestamp - axis_layout.base_timestamp)

    indexed_times = [
        (idx, usage.timestamp)
        for idx, usage in enumerate(usage_series)
        if usage.timestamp is not None
    ]
    if not indexed_times:
        return None

    timestamps = [timestamp for _, timestamp in indexed_times]
    insert_at = bisect_left(timestamps, marker.timestamp)

    candidates: list[tuple[int, float]] = []
    if insert_at < len(indexed_times):
        candidates.append(indexed_times[insert_at])
    if insert_at > 0:
        candidates.append(indexed_times[insert_at - 1])
    if not candidates:
        return None

    nearest_idx, _ = min(candidates, key=lambda item: abs(item[1] - marker.timestamp))
    return float(nearest_idx + 1)


def nearest_usage_index(
    usage_series: list[UsageData],
    timestamp: float | None,
) -> int | None:
    """Return the usage index nearest to the provided timestamp."""
    if timestamp is None or not usage_series:
        return None

    indexed_times = [
        (idx, usage.timestamp)
        for idx, usage in enumerate(usage_series)
        if usage.timestamp is not None
    ]
    if not indexed_times:
        return None

    timestamps = [ts for _, ts in indexed_times]
    insert_at = bisect_left(timestamps, timestamp)

    candidates: list[tuple[int, float]] = []
    if insert_at < len(indexed_times):
        candidates.append(indexed_times[insert_at])
    if insert_at > 0:
        candidates.append(indexed_times[insert_at - 1])
    if not candidates:
        return None

    nearest_idx, _ = min(candidates, key=lambda item: abs(item[1] - timestamp))
    return nearest_idx


def trailing_window_total(
    usage_series: list[UsageData],
    cursor_index: int,
    *,
    hours: int = 5,
) -> int | None:
    """Infer a trailing-window total from local usage records."""
    if not usage_series or not (0 <= cursor_index < len(usage_series)):
        return None

    cursor_ts = usage_series[cursor_index].timestamp
    if cursor_ts is None:
        return None

    lower_bound = cursor_ts - (hours * 3600)
    total = 0
    for usage in usage_series:
        if usage.timestamp is None:
            continue
        if lower_bound <= usage.timestamp <= cursor_ts:
            total += usage.total_tokens
    return total
