"""Screen 2: Session detail with conversation, token usage, and cache analysis tabs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from ..models import ParsedSession, SessionIndex
from ..parser import build_parsed_session
from ..widgets.cache_chart import CacheChart
from ..widgets.record_table import RecordHighlighted, RecordTable
from ..widgets.token_chart import TokenChart
from ..widgets.usage_panel import UsagePanel


class SessionDetailScreen(Screen):
    """Detail view for a single session with tabbed content."""

    TAB_IDS = ["tab-conversation", "tab-tokens", "tab-cache"]

    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("f", "toggle_full", "Toggle full/last-10"),
        ("q", "go_back", "Back"),
        ("1", "switch_tab('tab-conversation')", "Conversation"),
        ("2", "switch_tab('tab-tokens')", "Token Usage"),
        ("3", "switch_tab('tab-cache')", "Cache Analysis"),
        ("tab", "next_tab", "Next tab"),
        ("shift+tab", "prev_tab", "Prev tab"),
    ]

    def __init__(self, index: SessionIndex):
        super().__init__()
        self.index = index
        self.parsed: ParsedSession | None = None

    def compose(self) -> ComposeResult:
        slug = self.index.slug or "(unnamed)"
        yield Static(
            f"Session: {self.index.session_id}  |  "
            f"{slug}  |  PID: {self.index.pid}",
            id="detail-header",
        )

        with TabbedContent("Conversation", "Token Usage", "Cache Analysis"):
            with TabPane("Conversation", id="tab-conversation"):
                yield Static("Loading...", id="loading-indicator")

            with TabPane("Token Usage", id="tab-tokens"):
                yield Static("Loading...", id="loading-tokens")

            with TabPane("Cache Analysis", id="tab-cache"):
                yield Static("Loading...", id="loading-cache")

        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load_data)

    def _load_data(self) -> None:
        app = self.app
        truncate_at = getattr(app, "truncate_at", 200)
        last_n = getattr(app, "last_n", 10)

        self.parsed = build_parsed_session(self.index, truncate_at=truncate_at)

        # --- Conversation tab ---
        conv_tab = self.query_one("#tab-conversation", TabPane)
        loading = self.query_one("#loading-indicator", Static)
        loading.remove()

        dynamic = self.parsed.dynamic_records
        record_table = RecordTable(dynamic, last_n=last_n, id="record-table")
        conv_tab.mount(record_table)

        # Common records summary
        summary = self.parsed.common_record_summary()
        if summary:
            parts = [f"{count} {rtype}" for rtype, count in sorted(summary.items())]
            summary_text = "Common Records: " + ", ".join(parts)
        else:
            summary_text = "Common Records: none"
        conv_tab.mount(Static(summary_text, id="common-summary"))

        # Usage panel (hidden by default)
        conv_tab.mount(UsagePanel())

        # --- Token Usage tab ---
        tokens_tab = self.query_one("#tab-tokens", TabPane)
        loading_t = self.query_one("#loading-tokens", Static)
        loading_t.remove()
        usage_series = self.parsed.usage_series
        tokens_tab.mount(TokenChart(usage_series, id="chart-container"))

        # --- Cache Analysis tab ---
        cache_tab = self.query_one("#tab-cache", TabPane)
        loading_c = self.query_one("#loading-cache", Static)
        loading_c.remove()
        cache_tab.mount(CacheChart(usage_series, id="cache-container"))

        total_records = len(self.parsed.records)
        self.notify(
            f"Loaded {total_records} records "
            f"({len(dynamic)} dynamic, {len(usage_series)} with usage)",
            timeout=3,
        )

        # Ensure Conversation tab is active and record table is focused
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-conversation"
        self.call_after_refresh(lambda: record_table.focus())

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Focus the right widget and redraw charts when switching tabs."""
        tab_id = event.pane.id
        if tab_id == "tab-conversation":
            try:
                self.query_one(RecordTable).focus()
            except Exception:
                pass
        elif tab_id == "tab-tokens":
            try:
                tc = self.query_one(TokenChart)
                tc.focus()
                self.call_after_refresh(tc._redraw)
            except Exception:
                pass
        elif tab_id == "tab-cache":
            try:
                cc = self.query_one(CacheChart)
                cc.focus()
                self.call_after_refresh(cc._redraw)
            except Exception:
                pass

    def on_record_highlighted(self, event: RecordHighlighted) -> None:
        usage_panel = self.query_one(UsagePanel)
        if event.record and event.record.usage:
            usage_panel.show_usage(event.record.usage)
        else:
            usage_panel.hide_usage()

    def action_toggle_full(self) -> None:
        try:
            record_table = self.query_one(RecordTable)
            record_table.toggle_full()
            mode = "all" if record_table.show_all else f"last {record_table.last_n}"
            self.notify(f"Showing {mode} records")
        except Exception:
            pass

    def action_switch_tab(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = tab_id

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        current = tabs.active
        idx = self.TAB_IDS.index(current) if current in self.TAB_IDS else 0
        tabs.active = self.TAB_IDS[(idx + 1) % len(self.TAB_IDS)]

    def action_prev_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        current = tabs.active
        idx = self.TAB_IDS.index(current) if current in self.TAB_IDS else 0
        tabs.active = self.TAB_IDS[(idx - 1) % len(self.TAB_IDS)]

    def action_go_back(self) -> None:
        self.app.pop_screen()
