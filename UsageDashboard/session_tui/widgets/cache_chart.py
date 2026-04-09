"""Cache token analysis chart with range selection.

Cache fields in Claude Code JSONL:
  - cache_creation_input_tokens: Total tokens written to cache this request
  - cache_read_input_tokens:     Total tokens read from cache (cache hits)
  - ephemeral_5m_input_tokens:   Tokens cached with 5-minute TTL (rapid turns)
  - ephemeral_1h_input_tokens:   Tokens cached with 1-hour TTL (system prompts, stable context)

Formula:
  cache_creation_input_tokens = ephemeral_5m_input_tokens + ephemeral_1h_input_tokens
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual_plotext import PlotextPlot

from ..models import UsageData
from .range_summary import RangeSummary

CACHE_DESCRIPTION = (
    "cache_creation = tokens written to cache  |  "
    "cache_read = tokens read from cache (hits)\n"
    "cache_creation = ephemeral_5m + ephemeral_1h  |  "
    "5m = rapid turn cache, 1h = stable context cache"
)


class CacheChart(Widget):
    """PlotextPlot showing all 4 cache token series over time."""

    can_focus = True

    show_creation = reactive(True)
    show_read = reactive(True)
    show_eph_5m = reactive(True)
    show_eph_1h = reactive(True)
    cursor_pos = reactive(0)
    range_start = reactive(-1)
    range_end = reactive(-1)

    BINDINGS = [
        ("w", "toggle_creation", "Toggle creation"),
        ("r", "toggle_read", "Toggle read"),
        ("5", "toggle_eph_5m", "Toggle 5m"),
        ("h", "toggle_eph_1h", "Toggle 1h"),
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
        yield Static(CACHE_DESCRIPTION, id="cache-description")
        yield PlotextPlot(id="cache-plot")
        yield Static("", id="cache-legend", classes="chart-legend")
        yield RangeSummary(id="cache-range-summary")

    def on_mount(self) -> None:
        self._update_legend()
        self.query_one(RangeSummary).clear_range()
        self.call_after_refresh(self._redraw)

    def _update_legend(self) -> None:
        w = "ON" if self.show_creation else "off"
        r = "ON" if self.show_read else "off"
        e5 = "ON" if self.show_eph_5m else "off"
        e1 = "ON" if self.show_eph_1h else "off"
        total_create = sum(u.cache_creation_input_tokens for u in self.usage_series)
        total_read = sum(u.cache_read_input_tokens for u in self.usage_series)
        ratio = f"{total_read / total_create:.2f}" if total_create > 0 else "N/A"
        legend = self.query_one("#cache-legend", Static)
        legend.update(
            f"[w] creation:{w}  [r] read:{r}  "
            f"[5] eph_5m:{e5}  [h] eph_1h:{e1}  |  "
            f"ratio: {ratio} read/create  |  "
            f"Left/Right  [ ]  x"
        )

    def _redraw(self) -> None:
        plot_widget = self.query_one("#cache-plot", PlotextPlot)
        plt = plot_widget.plt
        plt.clear_figure()
        plt.clear_data()

        if not self.usage_series:
            plt.title("No cache data")
            plot_widget.refresh()
            return

        x = list(range(1, len(self.usage_series) + 1))
        all_y: list[int] = []

        if self.show_creation:
            y = [round(u.cache_creation_input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="cache_creation", marker="braille", color="cyan")
            all_y.extend(y)

        if self.show_read:
            y = [round(u.cache_read_input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="cache_read", marker="braille", color="green")
            all_y.extend(y)

        if self.show_eph_5m:
            y = [round(u.ephemeral_5m_input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="eph_5m", marker="braille", color="yellow")
            all_y.extend(y)

        if self.show_eph_1h:
            y = [round(u.ephemeral_1h_input_tokens / 1000) for u in self.usage_series]
            plt.plot(x, y, label="eph_1h", marker="braille", color="magenta")
            all_y.extend(y)

        # Set integer Y-ticks in 1K steps
        if all_y:
            max_y = max(all_y) or 1
            step = max(1, max_y // 8)
            yticks = list(range(0, max_y + step + 1, step))
            plt.yticks(yticks)

        # Range markers
        if self.range_start >= 0 and self.range_start < len(self.usage_series):
            plt.vline(self.range_start + 1, color="red")
        if self.range_end >= 0 and self.range_end < len(self.usage_series):
            plt.vline(self.range_end + 1, color="red")

        # Cursor
        if 0 <= self.cursor_pos < len(self.usage_series):
            plt.vline(self.cursor_pos + 1, color="green")

        plt.title("Cache Token Analysis")
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
            summary.update_range(usage_slice, s, e, mode="cache")
        else:
            summary.clear_range()

    def on_resize(self, event) -> None:
        self._redraw()

    def watch_show_creation(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_read(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_eph_5m(self, _: bool) -> None:
        self._update_legend()
        self._redraw()

    def watch_show_eph_1h(self, _: bool) -> None:
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

    def action_toggle_creation(self) -> None:
        self.show_creation = not self.show_creation

    def action_toggle_read(self) -> None:
        self.show_read = not self.show_read

    def action_toggle_eph_5m(self) -> None:
        self.show_eph_5m = not self.show_eph_5m

    def action_toggle_eph_1h(self) -> None:
        self.show_eph_1h = not self.show_eph_1h

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
