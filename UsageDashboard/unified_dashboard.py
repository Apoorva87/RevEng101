#!/usr/bin/env python3
"""Unified session dashboard aggregating Claude and Codex sessions."""

from __future__ import annotations

import argparse
import json
import time
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import webbrowser

from providers.base import SessionProvider, NormalizedSession, classify_activity
from providers.claude_provider import ClaudeProvider
from providers.codex_provider import CodexProvider

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8878
STATIC_DIR = Path(__file__).resolve().parent / "static"
INACTIVITY_THRESHOLD_DAYS = 3

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified session dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--claude-root", type=Path, default=None, help="Claude root dir (~/.claude)")
    parser.add_argument("--codex-sessions", type=Path, default=None, help="Codex sessions dir (~/.codex/sessions)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--inactivity-days", type=int, default=INACTIVITY_THRESHOLD_DAYS, help="Days of inactivity before marking session inactive.")
    return parser.parse_args()


class SessionHub:
    """Aggregates sessions from multiple providers."""

    def __init__(self, providers: list[SessionProvider]):
        self.providers = providers
        self._lock = threading.RLock()

    def scan_all(self) -> dict[str, Any]:
        with self._lock:
            all_sessions: list[NormalizedSession] = []
            provider_info: list[dict[str, str]] = []
            errors: list[dict[str, str]] = []

            for provider in self.providers:
                provider_info.append({
                    "id": provider.provider_id,
                    "label": provider.provider_label,
                })
                try:
                    sessions = provider.scan()
                    all_sessions.extend(sessions)
                except Exception as exc:
                    errors.append({
                        "provider": provider.provider_id,
                        "error": str(exc),
                    })

            # Sort: running first, then blocked, then by last_activity descending
            category_order = {"running": 0, "blocked": 1, "idle": 2}
            all_sessions.sort(
                key=lambda s: (
                    category_order.get(s.state_category, 9),
                    -(s.last_activity_at or 0),
                ),
            )

            active_sessions = [
                s for s in all_sessions
                if classify_activity(s.last_activity_at) == "active"
                or s.state_category == "running"
            ]

            # Compute metrics
            total_tokens = sum(s.total_tokens for s in all_sessions)
            claude_tokens = sum(s.total_tokens for s in all_sessions if s.provider == "claude")
            codex_tokens = sum(s.total_tokens for s in all_sessions if s.provider == "codex")
            projects = len({s.project_name for s in all_sessions})
            rate_limited = sum(1 for s in all_sessions if s.state == "rate_limited")
            error_count = sum(1 for s in all_sessions if s.state == "error")

            return {
                "generated_at": time.time(),
                "providers": provider_info,
                "errors": errors,
                "metrics": {
                    "total_sessions": len(all_sessions),
                    "active_now": len(active_sessions),
                    "rate_limited": rate_limited,
                    "errors": error_count,
                    "total_tokens": total_tokens,
                    "claude_tokens": claude_tokens,
                    "codex_tokens": codex_tokens,
                    "projects": projects,
                },
                "active_sessions": [_session_dict(s) for s in active_sessions],
                "sessions": [_session_dict(s) for s in all_sessions],
            }

    def raw_provider_snapshot(self, provider_id: str) -> dict[str, Any] | None:
        with self._lock:
            for provider in self.providers:
                if provider.provider_id == provider_id:
                    return provider.raw_snapshot()
        return None

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session by its ID, delegating to the appropriate provider."""
        with self._lock:
            for provider in self.providers:
                if hasattr(provider, "delete_session"):
                    result = provider.delete_session(session_id)
                    if "error" not in result or "not found" not in result.get("error", "").lower():
                        return result
        return {"error": f"Session {session_id} not found in any provider"}

    def delete_inactive_sessions(self, inactivity_days: int = INACTIVITY_THRESHOLD_DAYS) -> dict[str, Any]:
        """Delete all sessions inactive beyond threshold across all providers."""
        threshold = time.time() - inactivity_days * 86400
        data = self.scan_all()
        deleted = []
        errors = []
        for s in data.get("sessions", []):
            last_ts = s.get("last_activity_at") or 0
            if last_ts > 0 and last_ts < threshold and s.get("state_category") == "idle":
                result = self.delete_session(s["session_id"])
                if "error" in result:
                    errors.append(result["error"])
                else:
                    deleted.append(s["session_id"])
        return {"deleted": len(deleted), "errors": errors}


def _session_dict(s: NormalizedSession) -> dict[str, Any]:
    d = asdict(s)
    d["activity"] = classify_activity(s.last_activity_at)
    return d


class UnifiedHandler(BaseHTTPRequestHandler):
    server: "UnifiedServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"error": "Not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._send_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            file_path = (STATIC_DIR / rel).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self._send_json({"error": "Forbidden"}, 403)
                return
            suffix = file_path.suffix.lower()
            ct = MIME_TYPES.get(suffix, "application/octet-stream")
            self._send_file(file_path, ct)
            return

        if path == "/api/sessions":
            try:
                self._send_json(self.server.hub.scan_all())
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        if path.startswith("/api/provider/"):
            parts = path.split("/")
            if len(parts) >= 5 and parts[4] == "raw":
                provider_id = parts[3]
                result = self.server.hub.raw_provider_snapshot(provider_id)
                if result is None:
                    self._send_json({"error": f"Unknown provider: {provider_id}"}, 404)
                else:
                    self._send_json(result)
                return

        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path.startswith("/api/sessions/") and path != "/api/sessions/inactive":
            session_id = path[len("/api/sessions/"):]
            if not session_id:
                self._send_json({"error": "Missing session_id"}, 400)
                return
            result = self.server.hub.delete_session(session_id)
            self._send_json(result, 200 if "error" not in result else 400)
            return

        if path == "/api/sessions/inactive":
            from urllib.parse import parse_qs as _pq
            params = _pq(parsed.query)
            days = int((params.get("days") or [str(INACTIVITY_THRESHOLD_DAYS)])[0])
            result = self.server.hub.delete_inactive_sessions(days)
            self._send_json(result)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class UnifiedServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], hub: SessionHub):
        super().__init__(address, UnifiedHandler)
        self.hub = hub


def main() -> int:
    args = parse_args()

    providers: list[SessionProvider] = []
    providers.append(ClaudeProvider(args.claude_root))
    providers.append(CodexProvider(args.codex_sessions))

    hub = SessionHub(providers)
    server = UnifiedServer((args.host, args.port), hub)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Unified Session Dashboard listening at {url}", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
