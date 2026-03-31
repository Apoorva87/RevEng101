"""Base abstractions for session providers."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

INACTIVITY_THRESHOLD_DAYS = 3


@dataclass
class NormalizedSession:
    """Unified session representation across all providers."""

    provider: str
    session_id: str
    display_label: str
    project_name: str
    working_path: str
    account_key: str
    account_label: str
    first_activity_at: float | None  # unix timestamp
    last_activity_at: float | None  # unix timestamp
    state: str  # normalized state string
    raw_state: str  # original provider state
    state_category: str  # running / blocked / idle
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_uses: int = 0
    last_prompt: str | None = None
    last_error: str | None = None
    resume_command: str | None = None
    open_command: str | None = None
    duration_seconds: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def classify_activity(ts: float | None) -> str:
    """Classify recency of activity: active (<5min), recent (<1hr), idle."""
    if ts is None:
        return "idle"
    age = time.time() - ts
    if age < 300:
        return "active"
    if age < 3600:
        return "recent"
    return "idle"


def state_category(state: str) -> str:
    """Map a normalized state to a category: running, blocked, or idle."""
    running = {"awaiting_assistant", "awaiting_tool_result"}
    blocked = {"rate_limited", "error"}
    if state in running:
        return "running"
    if state in blocked:
        return "blocked"
    return "idle"


def apply_inactivity(session: NormalizedSession, threshold_days: int = INACTIVITY_THRESHOLD_DAYS) -> NormalizedSession:
    """Mark a session as inactive if its last activity exceeds the threshold."""
    if session.last_activity_at is None:
        return session
    age = time.time() - session.last_activity_at
    if age > threshold_days * 86400 and session.state_category == "idle":
        session.state = "inactive"
        session.state_category = "idle"
    return session


class SessionProvider(ABC):
    """Abstract base for a provider that can enumerate sessions."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        ...

    @property
    @abstractmethod
    def provider_label(self) -> str:
        ...

    @abstractmethod
    def scan(self) -> list[NormalizedSession]:
        ...

    @abstractmethod
    def raw_snapshot(self) -> dict[str, Any]:
        ...

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session. Override in subclasses that support deletion."""
        return {"error": f"Delete not supported for provider {self.provider_id}"}


# ── Phase 6: Permissions Routing (Interface Only - NOT implemented) ──
# Behind --enable-permissions flag (default off).
# Planned endpoints:
#   GET  /api/permissions         → list pending permission requests
#   POST /api/permissions/<id>/approve  → approve a permission request
#   POST /api/permissions/<id>/deny     → deny a permission request
# Planned UI: .permissions-strip below active strip, amber-themed cards
#   with approve/deny buttons.

@dataclass
class PermissionRequest:
    """Data model for a pending tool permission request (Phase 6 - not yet implemented)."""

    request_id: str
    session_id: str
    provider: str
    tool_name: str
    description: str
    requested_at: float  # unix timestamp
    status: str = "pending"  # pending / approved / denied
