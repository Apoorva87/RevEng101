"""Token usage chart with range selection."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual_plotext import PlotextPlot

from ..models import UsageData
from .range_summary import RangeSummary


class TokenChart(Widget):
    """PlotextPlot showing input/output/cache tokens over time with range selection."""

    can_focus = True

    show_input = reactive(True)
    show_output = reactive(True)
    show_cache = reactive(True)
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
        ("x", "clear_range", "Clear range"),
    ]

    def __init__(self, usage_series: list[UsageData], **kwargs):
        super().__init__(**kwargs)
        self.usage_series = usage_series

    def compose(self) -> ComposeResult:
        yield PlotextPlot(id="token-plot")
        yield Static("", id="token-legend", classes="chart-legend")
        yield RangeSummary(id="token-range-summary")

    def on_mount(self) -> None:
        self._update_legend()
        self.query_one(RangeSummary).clear_range()
        self.call_after_refresh(self._redraw)

    def _update_legend(self) -> None:
        i_mark = "ON" if self.show_input else "off"
        o_mark = "ON" if self.show_output else "off"
        c_mark = "ON" if self.show_cache else "off"
        legend = self.query_one("#token-legend", Static)
        legend.update(
            f"[i]nput:{i_mark}  [o]utput:{o_mark}  [c]ache_read:{c_mark}  |  "
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

        x = list(range(1, len(self.usage_series) + 1))

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

        # Draw range markers as vertical lines
        if self.range_start >= 0 and self.range_start < len(self.usage_series):
            plt.vline(self.range_start + 1, color="red")
        if self.range_end >= 0 and self.range_end < len(self.usage_series):
            plt.vline(self.range_end + 1, color="red")

        # Draw cursor
        if 0 <= self.cursor_pos < len(self.usage_series):
            plt.vline(self.cursor_pos + 1, color="green")

        plt.title("Token Usage Over Time")
        plt.xlabel("Message #")
        plt.ylabel("Tokens (K)")
        plot_widget.refresh()

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

    def watch_show_input(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_output(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_cache(self, _: bool) -> None:
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
