"""Screen 1: Sessions list with preview panel."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ..parser import format_relative, load_session_indexes
from ..models import SessionIndex


class SessionsListScreen(Screen):
    """Shows all sessions from ~/.claude/sessions/ in a sortable table."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("escape", "quit", "Quit"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, claude_root: Path):
        super().__init__()
        self.claude_root = claude_root
        self.sessions: list[SessionIndex] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="sessions-table", cursor_type="row")
        yield Static("", id="preview-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._load_sessions()

    def _load_sessions(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.clear(columns=True)
        table.add_columns("PID", "Session Name", "CWD", "Started", "Kind")

        self.sessions = load_session_indexes(self.claude_root)

        for idx in self.sessions:
            name = idx.slug or "(unnamed)"
            if len(name) > 25:
                name = name[:22] + "..."

            cwd = idx.cwd
            if len(cwd) > 35:
                cwd = "..." + cwd[-32:]

            started = format_relative(idx.started_at)
            table.add_row(
                str(idx.pid),
                name,
                cwd,
                started,
                idx.entrypoint,
                key=idx.session_id,
            )

        self._update_preview()

    def on_data_table_cursor_moved(self, event: DataTable.CursorMoved) -> None:
        self._update_preview()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a table row."""
        self.action_open_session()

    def _update_preview(self) -> None:
        preview = self.query_one("#preview-panel", Static)
        table = self.query_one("#sessions-table", DataTable)

        if not self.sessions or table.cursor_row >= len(self.sessions):
            preview.update("No sessions found")
            return

        idx = self.sessions[table.cursor_row]
        has_jsonl = "Yes" if idx.jsonl_path else "No"

        preview.update(
            f"Session: {idx.session_id}  |  JSONL: {has_jsonl}\n"
            f"CWD: {idx.cwd}\n"
            f"PID: {idx.pid}  |  Kind: {idx.kind}  |  Entry: {idx.entrypoint}"
        )

    def action_open_session(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if not self.sessions or table.cursor_row >= len(self.sessions):
            return

        idx = self.sessions[table.cursor_row]
        if not idx.jsonl_path:
            self.notify("No JSONL file found for this session", severity="warning")
            return

        from .session_detail import SessionDetailScreen
        self.app.push_screen(SessionDetailScreen(idx))

    def action_refresh(self) -> None:
        self._load_sessions()
        self.notify("Sessions refreshed")

    def action_quit(self) -> None:
        self.app.exit()
