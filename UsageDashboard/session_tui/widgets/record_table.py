"""DataTable widget for displaying dynamic session records."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import DataTable

from ..models import SessionRecord, UsageData
from ..parser import format_ts_short


class RecordHighlighted(Message):
    """Emitted when a record row is highlighted."""

    def __init__(self, record: SessionRecord | None) -> None:
        super().__init__()
        self.record = record


class RecordTable(DataTable):
    """Table showing dynamic session records (user/assistant/system)."""

    def __init__(self, records: list[SessionRecord], last_n: int = 10, **kwargs):
        super().__init__(cursor_type="row", **kwargs)
        self.all_records = records
        self.last_n = last_n
        self.show_all = False
        self._displayed_records: list[SessionRecord] = []

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        self.clear(columns=True)
        self.add_columns("Time", "Type", "Preview")

        if self.show_all:
            self._displayed_records = list(self.all_records)
        else:
            self._displayed_records = self.all_records[-self.last_n:]

        for rec in self._displayed_records:
            time_str = format_ts_short(rec.timestamp)
            type_label = rec.type_label
            preview = rec.text_preview or ""
            if len(preview) > 120:
                preview = preview[:117] + "..."
            self.add_row(time_str, type_label, preview)

    def toggle_full(self) -> None:
        self.show_all = not self.show_all
        self._populate()

    def on_data_table_cursor_moved(self, event: DataTable.CursorMoved) -> None:
        if 0 <= self.cursor_row < len(self._displayed_records):
            record = self._displayed_records[self.cursor_row]
            self.post_message(RecordHighlighted(record))
        else:
            self.post_message(RecordHighlighted(None))

    def get_current_record(self) -> SessionRecord | None:
        if 0 <= self.cursor_row < len(self._displayed_records):
            return self._displayed_records[self.cursor_row]
        return None
