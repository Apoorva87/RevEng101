"""Token usage chart with range selection."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual_plotext import PlotextPlot

from ..chart_support import (
    build_axis_layout,
    build_time_ticks,
    marker_x_position,
    nearest_usage_index,
)
from ..models import ChartMarker, UsageData
from .marker_info import MarkerInfo
from .range_summary import RangeSummary


class TokenChart(Widget):
    """PlotextPlot showing input/output/cache tokens over time with range selection."""

    can_focus = True

    show_input = reactive(True)
    show_output = reactive(True)
    show_cache = reactive(True)
    axis_mode = reactive("message")
    show_markers = reactive(True)
    cursor_pos = reactive(0)
    range_start = reactive(-1)
    range_end = reactive(-1)

    BINDINGS = [
        ("i", "toggle_input", "Toggle input"),
        ("o", "toggle_output", "Toggle output"),
        ("c", "toggle_cache", "Toggle cache"),
        ("left", "cursor_left", "Cursor left"),
        ("right", "cursor_right", "Cursor right"),
        ("[", "set_range_start", "Set range start"),
        ("]", "set_range_end", "Set range end"),
        ("n", "next_marker", "Next marker"),
        ("p", "prev_marker", "Prev marker"),
        ("x", "clear_range", "Clear range"),
    ]

    def __init__(
        self,
        usage_series: list[UsageData],
        chart_markers: list[ChartMarker] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.usage_series = usage_series
        self.chart_markers = chart_markers or []

    def compose(self) -> ComposeResult:
        yield PlotextPlot(id="token-plot")
        yield Static("", id="token-legend", classes="chart-legend")
        yield MarkerInfo(id="token-marker-info", classes="marker-info")
        yield RangeSummary(id="token-range-summary")

    def on_mount(self) -> None:
        self._update_legend()
        self.query_one(RangeSummary).clear_range()
        self.call_after_refresh(self._redraw)

    def _update_legend(self) -> None:
        i_mark = "ON" if self.show_input else "off"
        o_mark = "ON" if self.show_output else "off"
        c_mark = "ON" if self.show_cache else "off"
        axis_label = "time" if self.axis_mode == "timeline" else "msg"
        markers_label = "ON" if self.show_markers else "off"
        launch_count = sum(1 for marker in self.chart_markers if marker.kind == "agent_launch")
        subagent_count = len({
            marker.agent_id or marker.label
            for marker in self.chart_markers
            if marker.kind.startswith("subagent_")
        })
        limit_count = sum(1 for marker in self.chart_markers if marker.kind == "rate_limit_hit")
        legend = self.query_one("#token-legend", Static)
        legend.update(
            f"[i]nput:{i_mark}  [o]utput:{o_mark}  [c]ache_read:{c_mark}  |  "
            f"[t] axis:{axis_label}  [m] markers:{markers_label}  "
            f"(launch:{launch_count} sub:{subagent_count} limit:{limit_count})  |  "
            f"Left/Right: cursor  [ ]: range  x: clear"
        )

    def _redraw(self) -> None:
        plot_widget = self.query_one("#token-plot", PlotextPlot)
        plt = plot_widget.plt
        plt.clear_figure()
        plt.clear_data()

        if not self.usage_series:
            plt.title("No usage data")
            plot_widget.refresh()
            return

        axis_layout = build_axis_layout(self.usage_series, self.axis_mode)
        x = axis_layout.x_values

        all_y: list[int] = []

        if self.show_input:
            y = [round(u.input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="input", marker="braille", color="cyan")
            all_y.extend(y)

        if self.show_output:
            y = [round(u.output_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="output", marker="braille", color="magenta")
            all_y.extend(y)

        if self.show_cache:
            y = [round(u.cache_read_input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="cache_read", marker="braille", color="green")
            all_y.extend(y)

        # Set integer Y-ticks in 1K steps
        if all_y:
            max_y = max(all_y) or 1
            step = max(1, max_y // 8)  # ~8 ticks
            yticks = list(range(0, max_y + step + 1, step))
            plt.yticks(yticks)

        marker_positions: list[tuple[str, float]] = []
        if self.show_markers:
            for marker in self.chart_markers:
                x_pos = marker_x_position(marker, self.usage_series, axis_layout)
                if x_pos is None:
                    continue
                marker_positions.append((marker.kind, x_pos))

            seen_positions: set[tuple[str, float]] = set()
            for kind, x_pos in marker_positions:
                dedupe_key = (kind, round(x_pos, 6))
                if dedupe_key in seen_positions:
                    continue
                seen_positions.add(dedupe_key)

                color = self._marker_color(kind)
                plt.vline(x_pos, color=color)

        # Draw range markers as vertical lines
        if self.range_start >= 0 and self.range_start < len(self.usage_series):
            plt.vline(x[self.range_start], color="red")
        if self.range_end >= 0 and self.range_end < len(self.usage_series):
            plt.vline(x[self.range_end], color="red")

        # Draw cursor
        if 0 <= self.cursor_pos < len(self.usage_series):
            plt.vline(x[self.cursor_pos], color="green")

        axis_positions = list(x)
        axis_positions.extend(position for _, position in marker_positions)
        if axis_positions:
            x_min = min(axis_positions)
            x_max = max(axis_positions)
            if x_min == x_max:
                x_min -= 1
                x_max += 1
            plt.xlim(x_min, x_max)

        if axis_layout.mode == "timeline":
            tick_min = min(axis_positions) if axis_positions else 0.0
            tick_max = max(axis_positions) if axis_positions else 0.0
            xticks, xlabels = build_time_ticks(tick_min, tick_max)
            plt.xticks(xticks, xlabels)
            plt.xfrequency(0)

        axis_title = "Elapsed Time" if axis_layout.mode == "timeline" else "Message Order"
        plt.title(f"Token Usage Over Time ({axis_title})")
        plt.xlabel(axis_layout.xlabel)
        plt.ylabel("Tokens (K)")
        plot_widget.refresh()
        self._update_marker_info(axis_layout)

    def _update_range_summary(self) -> None:
        summary = self.query_one(RangeSummary)
        if self.range_start >= 0 and self.range_end >= 0:
            s = min(self.range_start, self.range_end)
            e = max(self.range_start, self.range_end)
            e = min(e, len(self.usage_series) - 1)
            usage_slice = self.usage_series[s : e + 1]
            summary.update_range(usage_slice, s, e, mode="tokens")
        else:
            summary.clear_range()

    def on_resize(self, event) -> None:
        self._redraw()

    def _update_marker_info(self, axis_layout=None) -> None:
        if axis_layout is None:
            axis_layout = build_axis_layout(self.usage_series, self.axis_mode)
        marker_info = self.query_one(MarkerInfo)
        marker_info.update_for_cursor(
            usage_series=self.usage_series,
            chart_markers=self.chart_markers if self.show_markers else [],
            axis_layout=axis_layout,
            cursor_index=self.cursor_pos,
        )

    def _marker_color(self, kind: str) -> str:
        if kind == "agent_launch":
            return "yellow"
        if kind.startswith("subagent_"):
            return "blue"
        if kind == "rate_limit_hit":
            return "red"
        if kind == "rate_limit_reset":
            return "magenta"
        return "white"

    def _marker_targets(self) -> list[int]:
        targets = {
            idx
            for marker in self.chart_markers
            for idx in [nearest_usage_index(self.usage_series, marker.timestamp)]
            if idx is not None
        }
        return sorted(targets)

    def watch_show_input(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_output(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_cache(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_axis_mode(self, _: str) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_markers(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_cursor_pos(self, _: int) -> None:
        self._redraw()

    def watch_range_start(self, _: int) -> None:
        self._redraw()
        self._update_range_summary()

    def watch_range_end(self, _: int) -> None:
        self._redraw()
        self._update_range_summary()

    def action_toggle_input(self) -> None:
        self.show_input = not self.show_input

    def action_toggle_output(self) -> None:
        self.show_output = not self.show_output

    def action_toggle_cache(self) -> None:
        self.show_cache = not self.show_cache

    def action_cursor_left(self) -> None:
        if self.cursor_pos > 0:
            self.cursor_pos -= 1

    def action_cursor_right(self) -> None:
        if self.cursor_pos < len(self.usage_series) - 1:
            self.cursor_pos += 1

    def action_set_range_start(self) -> None:
        self.range_start = self.cursor_pos

    def action_set_range_end(self) -> None:
        self.range_end = self.cursor_pos

    def action_clear_range(self) -> None:
        self.range_start = -1
        self.range_end = -1

    def toggle_axis_mode(self) -> str:
        self.axis_mode = "timeline" if self.axis_mode == "message" else "message"
        return self.axis_mode

    def toggle_markers(self) -> bool:
        self.show_markers = not self.show_markers
        return self.show_markers

    def action_next_marker(self) -> None:
        for idx in self._marker_targets():
            if idx > self.cursor_pos:
                self.cursor_pos = idx
                return
        targets = self._marker_targets()
        if targets:
            self.cursor_pos = targets[0]

    def action_prev_marker(self) -> None:
        for idx in reversed(self._marker_targets()):
            if idx < self.cursor_pos:
                self.cursor_pos = idx
                return
        targets = self._marker_targets()
        if targets:
            self.cursor_pos = targets[-1]
