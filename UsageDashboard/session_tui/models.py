"""Data models for Claude session browsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionIndex:
    """One entry from ~/.claude/sessions/<pid>.json — maps a process to a session."""

    pid: int
    session_id: str
    cwd: str
    started_at: float  # epoch seconds
    kind: str  # "interactive"
    entrypoint: str  # "cli"
    slug: Optional[str] = None  # human-readable name, extracted from JSONL records
    jsonl_path: Optional[str] = None  # resolved path to the JSONL file


@dataclass
class UsageData:
    """Token usage breakdown from a single assistant message."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    model: str = "unknown"
    service_tier: Optional[str] = None
    speed: Optional[str] = None
    timestamp: Optional[float] = None
    record_uuid: Optional[str] = None
    request_id: Optional[str] = None
    agent_id: Optional[str] = None
    is_sidechain: bool = False
    source: str = "main"

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


@dataclass
class SessionRecord:
    """One parsed line from a session JSONL file."""

    type: str  # user, assistant, system, progress, file-history-snapshot, etc.
    timestamp: Optional[float]  # epoch seconds
    uuid: Optional[str]
    text_preview: Optional[str]  # truncated message content
    usage: Optional[UsageData]  # only set for assistant records with usage
    is_dynamic: bool  # True for user/assistant/system; False for metadata records
    raw: dict  # full parsed JSON for deep inspection

    @property
    def type_label(self) -> str:
        labels = {
            "user": "USER",
            "assistant": "ASST",
            "system": "SYS",
            "progress": "PROG",
            "file-history-snapshot": "SNAP",
            "last-prompt": "LAST",
            "attachment": "ATCH",
            "permission-mode": "PERM",
            "queue-operation": "QUEUE",
        }
        return labels.get(self.type, self.type.upper()[:5])


DYNAMIC_TYPES = {"user", "assistant", "system"}


@dataclass
class ChartMarker:
    """Vertical chart marker for agent launches or subagent activity."""

    kind: str  # "agent_launch", "subagent_start", "subagent_end"
    timestamp: Optional[float]
    label: str
    agent_id: Optional[str] = None
    total_tokens: int = 0
    detail: Optional[str] = None
    reset_at: Optional[float] = None


@dataclass
class ParsedSession:
    """Fully parsed session data, ready for display."""

    index: SessionIndex
    records: list[SessionRecord] = field(default_factory=list)
    chart_markers: list[ChartMarker] = field(default_factory=list)

    @property
    def dynamic_records(self) -> list[SessionRecord]:
        return [r for r in self.records if r.is_dynamic]

    @property
    def common_records(self) -> list[SessionRecord]:
        return [r for r in self.records if not r.is_dynamic]

    @property
    def usage_series(self) -> list[UsageData]:
        """Ordered usage data, collapsing streaming continuations by request ID."""
        series: list[UsageData] = []
        for record in self.records:
            usage = record.usage
            if usage is None or usage.total_tokens <= 0:
                continue

            if (
                series
                and usage.request_id
                and series[-1].request_id == usage.request_id
                and series[-1].source == usage.source
            ):
                series[-1] = usage
            else:
                series.append(usage)

        return series

    def common_record_summary(self) -> dict[str, int]:
        """Count of each common record type."""
        counts: dict[str, int] = {}
        for r in self.common_records:
            counts[r.type] = counts.get(r.type, 0) + 1
        return counts
