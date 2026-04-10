"""Bottom summary panel for marker metadata near the chart cursor."""

from __future__ import annotations

from textual.widgets import Static

from ..chart_support import (
    AxisLayout,
    format_elapsed_label,
    format_local_timestamp,
    marker_x_position,
    trailing_window_total,
)
from ..models import ChartMarker, UsageData


def _fmt(n: int) -> str:
    return f"{n:,}"


class MarkerInfo(Static):
    """Shows inferred trailing-window stats and nearby marker details."""

    def update_for_cursor(
        self,
        *,
        usage_series: list[UsageData],
        chart_markers: list[ChartMarker],
        axis_layout: AxisLayout,
        cursor_index: int,
    ) -> None:
        if not usage_series or not (0 <= cursor_index < len(usage_series)):
            self.update("No usage point selected")
            return

        cursor_usage = usage_series[cursor_index]
        cursor_x = axis_layout.x_values[cursor_index]
        cursor_time = format_local_timestamp(cursor_usage.timestamp)
        trailing = trailing_window_total(usage_series, cursor_index, hours=5)

        header = f"Cursor: msg{cursor_index + 1}"
        if axis_layout.mode == "timeline":
            header += f"  |  {cursor_time}  |  elapsed: {format_elapsed_label(cursor_x)}"
        else:
            header += f"  |  {cursor_time}"

        if trailing is not None:
            header += f"  |  inferred trailing 5h main-session tokens: {_fmt(trailing)}"
        else:
            header += "  |  inferred trailing 5h main-session tokens: -"

        if not chart_markers:
            self.update(f"{header}\nMarkers: none in this session")
            return

        positioned: list[tuple[ChartMarker, float, float]] = []
        for marker in chart_markers:
            x_pos = marker_x_position(marker, usage_series, axis_layout)
            if x_pos is None:
                continue
            positioned.append((marker, x_pos, abs(x_pos - cursor_x)))

        if not positioned:
            self.update(f"{header}\nMarkers: none with coordinates on this chart")
            return

        exact_matches = [
            (marker, distance)
            for marker, _, distance in positioned
            if distance < 1e-6
        ]
        nearest = sorted(positioned, key=lambda item: item[2])[:2]

        lines: list[str] = [header]
        if exact_matches:
            lines.append(
                "At cursor: " + "  |  ".join(self._format_marker(marker, 0.0, axis_layout.mode) for marker, _ in exact_matches[:2])
            )
        else:
            lines.append(
                "Nearest marker: " + "  |  ".join(
                    self._format_marker(marker, distance, axis_layout.mode)
                    for marker, _, distance in nearest
                )
            )
        lines.append("Marker keys: n/p jump markers  m show/hide markers  t toggle x-axis")
        self.update("\n".join(lines))

    def _format_marker(self, marker: ChartMarker, distance: float, axis_mode: str) -> str:
        parts = [marker.label]

        if marker.kind == "rate_limit_hit":
            parts.append("limit hit")
        elif marker.kind == "rate_limit_reset":
            parts.append("reset")
        elif marker.kind.startswith("subagent_"):
            parts.append("subagent")
        elif marker.kind == "agent_launch":
            parts.append("launch")

        if marker.timestamp is not None:
            parts.append(format_local_timestamp(marker.timestamp))

        if distance > 1e-6:
            if axis_mode == "timeline":
                parts.append(f"{format_elapsed_label(distance)} away")
            else:
                parts.append(f"{int(round(distance))} msg away")

        if marker.total_tokens:
            parts.append(f"tokens {_fmt(marker.total_tokens)}")

        if marker.reset_at is not None:
            parts.append(f"resets {format_local_timestamp(marker.reset_at)}")

        if marker.agent_id:
            parts.append(f"agent {marker.agent_id[:12]}")

        if marker.detail and "You've hit your limit" not in marker.detail:
            parts.append(marker.detail[:80])

        return " | ".join(parts)
