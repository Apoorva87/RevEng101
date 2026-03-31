"""Claude Code session provider for the unified dashboard."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

# Add parent directory to path so we can import the existing dashboard module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_sessions_dashboard import ClaudeSessionAnalyzer

from providers.base import NormalizedSession, SessionProvider, state_category


class ClaudeProvider(SessionProvider):
    """Wraps ClaudeSessionAnalyzer to produce NormalizedSession objects."""

    def __init__(self, root_dir: Path | None = None):
        self._root = (root_dir or Path.home() / ".claude").expanduser()
        self._analyzer = ClaudeSessionAnalyzer(self._root)
        self._lock = threading.RLock()

    @property
    def provider_id(self) -> str:
        return "claude"

    @property
    def provider_label(self) -> str:
        return "Claude Code"

    def scan(self) -> list[NormalizedSession]:
        with self._lock:
            snapshot = self._analyzer.snapshot()
        sessions: list[NormalizedSession] = []
        for s in snapshot.get("sessions", []):
            tokens = s.get("tokens") or {}
            state = s.get("state") or "unknown"
            project_path = s.get("project_path") or s.get("project_key") or ""
            project_name = Path(project_path).name if project_path else s.get("project_key", "")
            sessions.append(
                NormalizedSession(
                    provider="claude",
                    session_id=s["session_id"],
                    display_label=s["session_id"][:12],
                    project_name=project_name or "(unknown)",
                    working_path=s.get("working_path") or project_path or "",
                    account_key=s.get("account_key") or "unknown",
                    account_label=s.get("account_label") or "Unknown",
                    first_activity_at=s.get("first_event_at"),
                    last_activity_at=s.get("last_access_at") or s.get("last_event_at"),
                    state=state,
                    raw_state=state,
                    state_category=state_category(state),
                    total_tokens=int(tokens.get("total_tokens") or 0),
                    input_tokens=int(tokens.get("read_tokens") or 0),
                    output_tokens=int(tokens.get("write_tokens") or 0),
                    user_messages=int(tokens.get("user_prompts") or 0),
                    assistant_messages=int(tokens.get("assistant_messages") or 0),
                    tool_uses=int(tokens.get("tool_uses") or 0),
                    last_prompt=s.get("last_prompt"),
                    last_error=s.get("last_error_text"),
                    resume_command=s.get("resume_command"),
                    open_command=s.get("open_command"),
                    duration_seconds=int(s.get("duration_seconds") or 0),
                    extra={
                        "models": s.get("models"),
                        "file_mtime": s.get("file_mtime"),
                        "session_file": s.get("session_file"),
                    },
                )
            )
        return sessions

    def raw_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._analyzer.snapshot()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self._analyzer.snapshot()
        session = next((s for s in snapshot.get("sessions", []) if s["session_id"] == session_id), None)
        if not session:
            return {"error": f"Session {session_id} not found"}
        session_file = session.get("session_file")
        if not session_file:
            return {"error": "No session file path available"}
        file_path = Path(session_file).resolve()
        projects_dir = self._analyzer.projects_dir.resolve()
        if not str(file_path).startswith(str(projects_dir)):
            return {"error": "Path traversal rejected"}
        if not file_path.exists():
            return {"error": "Session file does not exist"}
        try:
            file_path.unlink()
            parent = file_path.parent
            if parent != projects_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            return {"deleted": 1, "session_id": session_id, "files": [str(file_path)]}
        except Exception as exc:
            return {"error": str(exc)}
