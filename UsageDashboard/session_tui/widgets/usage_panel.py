"""Bottom-docked panel showing usage breakdown for the highlighted assistant record."""

from __future__ import annotations

from textual.widgets import Static

from ..models import UsageData


def _fmt(n: int) -> str:
    """Format a number with commas."""
    return f"{n:,}"


class UsagePanel(Static):
    """Shows token usage details. Hidden when no assistant record is selected."""

    def __init__(self, **kwargs):
        super().__init__("", id="usage-panel", **kwargs)

    def show_usage(self, usage: UsageData) -> None:
        self.remove_class("hidden")
        self.add_class("visible")
        self.update(
            f"input: {_fmt(usage.input_tokens)}  "
            f"cache_create: {_fmt(usage.cache_creation_input_tokens)}  "
            f"cache_read: {_fmt(usage.cache_read_input_tokens)}  "
            f"output: {_fmt(usage.output_tokens)}  "
            f"total: {_fmt(usage.total_tokens)}\n"
            f"model: {usage.model}  "
            f"tier: {usage.service_tier or '-'}  "
            f"speed: {usage.speed or '-'}\n"
            f"ephemeral_5m: {_fmt(usage.ephemeral_5m_input_tokens)}  "
            f"ephemeral_1h: {_fmt(usage.ephemeral_1h_input_tokens)}  "
            f"web_search: {usage.web_search_requests}  "
            f"web_fetch: {usage.web_fetch_requests}"
        )

    def hide_usage(self) -> None:
        self.remove_class("visible")
        self.update("")
