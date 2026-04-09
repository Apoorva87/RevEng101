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
class ParsedSession:
    """Fully parsed session data, ready for display."""

    index: SessionIndex
    records: list[SessionRecord] = field(default_factory=list)

    @property
    def dynamic_records(self) -> list[SessionRecord]:
        return [r for r in self.records if r.is_dynamic]

    @property
    def common_records(self) -> list[SessionRecord]:
        return [r for r in self.records if not r.is_dynamic]

    @property
    def usage_series(self) -> list[UsageData]:
        """Ordered list of usage data from assistant messages (for charts)."""
        return [r.usage for r in self.records if r.usage is not None]

    def common_record_summary(self) -> dict[str, int]:
        """Count of each common record type."""
        counts: dict[str, int] = {}
        for r in self.common_records:
            counts[r.type] = counts.get(r.type, 0) + 1
        return counts
