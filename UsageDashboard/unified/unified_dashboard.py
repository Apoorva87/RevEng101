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
from urllib.parse import parse_qs, urlparse
import webbrowser

from providers.base import SessionProvider, NormalizedSession, classify_activity
from providers.claude_provider import ClaudeProvider
from providers.codex_provider import CodexProvider
from usage_timeline import build_usage_breakdown, build_usage_timeline

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

    def usage_timeline(
        self,
        provider_id: str = "all",
        bucket: str = "hour",
        preset: str = "7d",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            snapshots = [provider.usage_snapshot() for provider in self.providers]
        return build_usage_timeline(
            snapshots,
            provider_filter=provider_id,
            bucket=bucket,
            preset=preset,
            start_raw=start,
            end_raw=end,
        )

    def usage_breakdown(
        self,
        provider_id: str = "all",
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            snapshots = [provider.usage_snapshot() for provider in self.providers]
        return build_usage_breakdown(
            snapshots,
            provider_filter=provider_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )

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
        """Delete every session whose last activity is older than the threshold.

        Includes idle *and* blocked (rate_limited / error) sessions — anything
        that isn't currently running. Sessions without a timestamp fall back to
        the session file's mtime so genuinely orphaned files are not stuck.
        """
        threshold = time.time() - inactivity_days * 86400
        data = self.scan_all()
        deleted: list[str] = []
        skipped: list[dict[str, Any]] = []
        errors: list[str] = []
        for s in data.get("sessions", []):
            if s.get("state_category") == "running":
                continue
            last_ts = s.get("last_activity_at") or 0
            if not last_ts:
                last_ts = (s.get("extra") or {}).get("file_mtime") or 0
            if not last_ts or last_ts >= threshold:
                continue
            result = self.delete_session(s["session_id"])
            if "error" in result:
                errors.append(f"{s['session_id']}: {result['error']}")
                skipped.append({"session_id": s["session_id"], "error": result["error"]})
            else:
                deleted.append(s["session_id"])
        return {
            "deleted": len(deleted),
            "deleted_ids": deleted,
            "errors": errors,
            "skipped": skipped,
        }

    def session_prompts(self, session_id: str) -> dict[str, Any] | None:
        """Return all human prompts for a session, trying each provider."""
        with self._lock:
            for provider in self.providers:
                getter = getattr(provider, "session_prompts", None)
                if not callable(getter):
                    continue
                result = getter(session_id)
                if result is not None:
                    result["provider"] = provider.provider_id
                    return result
        return None

    def project_aggregates(self) -> dict[str, Any]:
        """Aggregate totals per project across providers (token_analysis-style)."""
        data = self.scan_all()
        projects: dict[str, dict[str, Any]] = {}
        for s in data.get("sessions", []):
            key = s.get("project_name") or "(unknown)"
            bucket = projects.setdefault(key, {
                "project": key,
                "sessions": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "subagent_count": 0,
                "subagent_tokens": 0,
                "providers": set(),
                "last_activity_at": 0,
            })
            bucket["sessions"] += 1
            bucket["total_tokens"] += int(s.get("total_tokens") or 0)
            bucket["input_tokens"] += int(s.get("input_tokens") or 0)
            bucket["output_tokens"] += int(s.get("output_tokens") or 0)
            bucket["cache_read_tokens"] += int(s.get("cache_read_tokens") or 0)
            bucket["cache_creation_tokens"] += int(s.get("cache_creation_tokens") or 0)
            bucket["subagent_count"] += int(s.get("subagent_count") or 0)
            bucket["subagent_tokens"] += int(s.get("subagent_tokens") or 0)
            bucket["providers"].add(s.get("provider") or "")
            la = s.get("last_activity_at") or 0
            if la > bucket["last_activity_at"]:
                bucket["last_activity_at"] = la

        rows = []
        for bucket in projects.values():
            bucket["providers"] = sorted(p for p in bucket["providers"] if p)
            rows.append(bucket)
        rows.sort(key=lambda r: r["total_tokens"], reverse=True)
        return {"projects": rows, "generated_at": time.time()}


def _session_dict(s: NormalizedSession) -> dict[str, Any]:
    d = asdict(s)
    d["activity"] = classify_activity(s.last_activity_at)
    return d


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

        if path == "/api/usage":
            try:
                params = parse_qs(parsed.query)
                payload = self.server.hub.usage_timeline(
                    provider_id=(params.get("provider") or ["all"])[0],
                    bucket=(params.get("bucket") or ["hour"])[0],
                    preset=(params.get("preset") or ["7d"])[0],
                    start=(params.get("start") or [None])[0],
                    end=(params.get("end") or [None])[0],
                )
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        if path == "/api/usage-breakdown":
            try:
                params = parse_qs(parsed.query)
                start_ts = _opt_float((params.get("start_ts") or [None])[0])
                end_ts = _opt_float((params.get("end_ts") or [None])[0])
                if start_ts is None or end_ts is None:
                    self._send_json({"error": "start_ts and end_ts are required"}, 400)
                    return
                payload = self.server.hub.usage_breakdown(
                    provider_id=(params.get("provider") or ["all"])[0],
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
                self._send_json(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        if path == "/api/projects":
            try:
                self._send_json(self.server.hub.project_aggregates())
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        if path.startswith("/api/sessions/") and path.endswith("/prompts"):
            session_id = path[len("/api/sessions/"):-len("/prompts")]
            if not session_id:
                self._send_json({"error": "Missing session_id"}, 400)
                return
            result = self.server.hub.session_prompts(session_id)
            if result is None:
                self._send_json({"error": f"Session {session_id} not found"}, 404)
            else:
                self._send_json(result)
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
