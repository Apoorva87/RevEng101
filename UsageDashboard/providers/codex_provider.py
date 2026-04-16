"""Codex session provider for the unified dashboard."""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_sessions_dashboard import CodexSessionApp, DEFAULT_SESSIONS_DIR, DEFAULT_CONFIG_PATH

from providers.base import NormalizedSession, SessionProvider, state_category


def _infer_codex_state(session: dict[str, Any]) -> str:
    """Infer state from Codex session data (Codex has no explicit state)."""
    last_access = session.get("last_access")
    if last_access is None:
        return "unknown"
    if hasattr(last_access, "timestamp"):
        ts = last_access.timestamp()
    else:
        ts = float(last_access) if last_access else 0
    age = time.time() - ts
    if age < 300:  # < 5 min
        # Check message pattern: if last message is from user, assistant is working
        user_msgs = session.get("user_messages", 0)
        agent_msgs = session.get("agent_messages", 0)
        if user_msgs > agent_msgs:
            return "awaiting_assistant"
        return "waiting_for_user"
    return "idle"


class CodexProvider(SessionProvider):
    """Wraps CodexSessionApp to produce NormalizedSession objects."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        config_path: Path | None = None,
    ):
        self._sessions_dir = (sessions_dir or DEFAULT_SESSIONS_DIR).expanduser()
        self._config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
        self._app = CodexSessionApp(self._sessions_dir, self._config_path)
        self._lock = threading.RLock()

    @property
    def provider_id(self) -> str:
        return "codex"

    @property
    def provider_label(self) -> str:
        return "Codex CLI"

    def scan(self) -> list[NormalizedSession]:
        with self._lock:
            snapshot = self._app.build_snapshot(preset="all")
        sessions: list[NormalizedSession] = []
        for s in snapshot.get("sessions", []):
            lifetime = s.get("lifetime_usage") or {}
            state = _infer_codex_state(s)
            cwd = s.get("cwd") or ""
            project = s.get("project") or (Path(cwd).name if cwd else "(unknown)")

            last_access = s.get("last_access")
            if last_access and hasattr(last_access, "isoformat"):
                last_ts = last_access.timestamp() if hasattr(last_access, "timestamp") else None
            elif isinstance(last_access, str):
                from datetime import datetime, timezone
                try:
                    last_ts = datetime.fromisoformat(last_access.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    last_ts = None
            else:
                last_ts = None

            first_access = s.get("first_access")
            if first_access and hasattr(first_access, "timestamp"):
                first_ts = first_access.timestamp()
            elif isinstance(first_access, str):
                from datetime import datetime, timezone
                try:
                    first_ts = datetime.fromisoformat(first_access.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    first_ts = None
            else:
                first_ts = None

            sessions.append(
                NormalizedSession(
                    provider="codex",
                    session_id=s["session_id"],
                    display_label=s.get("label") or s["session_id"][:12],
                    project_name=project,
                    working_path=s.get("working_path") or cwd or "",
                    account_key=s.get("account_key") or "unknown",
                    account_label=s.get("account_label") or "Unknown",
                    first_activity_at=first_ts,
                    last_activity_at=last_ts,
                    state=state,
                    raw_state=state,
                    state_category=state_category(state),
                    total_tokens=int(lifetime.get("total_tokens") or 0),
                    input_tokens=int(lifetime.get("input_tokens") or 0),
                    output_tokens=int(lifetime.get("output_tokens") or 0),
                    user_messages=int(s.get("user_messages") or 0),
                    assistant_messages=int(s.get("agent_messages") or 0),
                    tool_uses=0,
                    last_prompt=s.get("last_user_message"),
                    last_error=None,
                    resume_command=s.get("resume_command"),
                    open_command=s.get("open_command"),
                    duration_seconds=int(s.get("duration_seconds") or 0),
                    extra={
                        "originator": s.get("originator"),
                        "cli_version": s.get("cli_version"),
                        "model_provider": s.get("model_provider"),
                        "context_window": s.get("context_window"),
                        "file_path": s.get("file_path"),
                    },
                )
            )
        return sessions

    def raw_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._app.build_snapshot(preset="all")

    def usage_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._app.usage_snapshot()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self._app.build_snapshot(preset="all")
        session = next((s for s in snapshot.get("sessions", []) if s["session_id"] == session_id), None)
        if not session:
            return {"error": f"Session {session_id} not found"}
        file_path_str = session.get("file_path")
        if not file_path_str:
            return {"error": "No session file path available"}
        file_path = Path(file_path_str).resolve()
        sessions_dir = self._sessions_dir.resolve()
        if not str(file_path).startswith(str(sessions_dir)):
            return {"error": "Path traversal rejected"}
        if not file_path.exists():
            return {"error": "Session file does not exist"}
        try:
            file_path.unlink()
            return {"deleted": 1, "session_id": session_id, "files": [str(file_path)]}
        except Exception as exc:
            return {"error": str(exc)}
