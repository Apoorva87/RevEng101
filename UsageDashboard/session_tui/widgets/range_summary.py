"""Shared widget for displaying token totals over a selected range."""

from __future__ import annotations

from textual.widgets import Static

from ..chart_support import format_elapsed_label, format_local_timestamp
from ..models import UsageData


def _fmt(n: int) -> str:
    return f"{n:,}"


class RangeSummary(Static):
    """Displays aggregate token totals for a selected range of messages."""

    def __init__(self, id: str = "range-summary", **kwargs):
        super().__init__("", id=id, **kwargs)

    def update_range(
        self,
        usage_slice: list[UsageData],
        start_idx: int,
        end_idx: int,
        mode: str = "tokens",
    ) -> None:
        if not usage_slice:
            self.update("No data in selected range")
            return

        header = f"Range: msg{start_idx + 1}-msg{end_idx + 1}"
        start_ts = usage_slice[0].timestamp
        end_ts = usage_slice[-1].timestamp
        if start_ts is not None and end_ts is not None:
            elapsed = max(0.0, end_ts - start_ts)
            header += (
                f"  |  {format_local_timestamp(start_ts)} -> {format_local_timestamp(end_ts)}"
                f"  |  span: {format_elapsed_label(elapsed)}"
            )

        if mode == "cache":
            total_create = sum(u.cache_creation_input_tokens for u in usage_slice)
            total_read = sum(u.cache_read_input_tokens for u in usage_slice)
            ratio = f"{total_read / total_create:.2f}" if total_create > 0 else "N/A"
            total_eph_5m = sum(u.ephemeral_5m_input_tokens for u in usage_slice)
            total_eph_1h = sum(u.ephemeral_1h_input_tokens for u in usage_slice)
            self.update(
                f"{header}  |  Selected totals:\n"
                f"cache_create: {_fmt(total_create)}  "
                f"cache_read: {_fmt(total_read)}  "
                f"ratio: {ratio} read/create\n"
                f"ephemeral_5m: {_fmt(total_eph_5m)}  "
                f"ephemeral_1h: {_fmt(total_eph_1h)}"
            )
        else:
            total_in = sum(u.input_tokens for u in usage_slice)
            total_out = sum(u.output_tokens for u in usage_slice)
            total_cache = sum(u.cache_read_input_tokens for u in usage_slice)
            total_all = sum(u.total_tokens for u in usage_slice)
            self.update(
                f"{header}  |  Selected totals:\n"
                f"input: {_fmt(total_in)}  "
                f"output: {_fmt(total_out)}  "
                f"cache_read: {_fmt(total_cache)}  "
                f"total: {_fmt(total_all)}"
            )

    def clear_range(self) -> None:
        self.update("Press [ to set start, ] to set end, x to clear range")
