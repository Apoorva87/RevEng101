"""Textual App for the Claude Session Browser."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from .screens.sessions_list import SessionsListScreen


class SessionBrowserApp(App):
    """Main application — launches the sessions list screen."""

    TITLE = "Claude Session Browser"

    CSS = """
    Screen {
        background: $surface;
    }

    /* Sessions list screen */
    #sessions-table {
        height: 1fr;
    }

    #preview-panel {
        height: 5;
        dock: bottom;
        border-top: solid $primary;
        padding: 0 1;
        background: $surface-darken-1;
    }

    #footer-bar {
        height: 1;
        dock: bottom;
        background: $primary;
        color: $text;
        padding: 0 1;
    }

    /* Session detail screen */
    #detail-header {
        height: 2;
        padding: 0 1;
        background: $primary;
        color: $text;
    }

    #record-table {
        height: 1fr;
    }

    #common-summary {
        height: 3;
        border-top: solid $accent;
        padding: 0 1;
        background: $surface-darken-1;
    }

    #usage-panel {
        height: 6;
        dock: bottom;
        border-top: solid $success;
        padding: 0 1;
        background: $surface-darken-2;
        display: none;
    }

    #usage-panel.visible {
        display: block;
    }

    /* Chart tabs */
    #chart-container, #cache-container {
        height: 1fr;
    }

    #range-summary {
        height: 4;
        dock: bottom;
        border-top: solid $warning;
        padding: 0 1;
        background: $surface-darken-1;
    }

    .chart-legend {
        height: 1;
        dock: bottom;
        padding: 0 1;
        background: $surface-darken-1;
    }

    #cache-description {
        height: 3;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text-muted;
        border-bottom: solid $accent;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        claude_root: Path = Path.home() / ".claude",
        truncate_at: int = 200,
        last_n: int = 10,
    ):
        super().__init__()
        self.claude_root = claude_root
        self.truncate_at = truncate_at
        self.last_n = last_n

    def on_mount(self) -> None:
        self.push_screen(SessionsListScreen(self.claude_root))
