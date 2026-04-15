"""In-memory and on-disk storage for request traces."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TraceStore:
    """Keeps recent traces in memory and persists them to disk in the background."""

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = 200,
        max_disk_entries: int = 1000,
        max_body_chars: int = 32_000,
    ) -> None:
        self._root = Path(root)
        self._detail_dir = self._root / "requests"
        self._index_path = self._root / "requests.jsonl"
        self._max_entries = max_entries
        self._max_disk_entries = max_disk_entries
        self._max_body_chars = max_body_chars
        self._recent: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._disk_entries: deque[dict[str, Any]] = deque()
        self._details: dict[str, dict[str, Any]] = {}
        self._persist_lock = asyncio.Lock()

    async def init(self) -> None:
        """Ensure on-disk directories exist and reload the recent index."""
        await asyncio.to_thread(self._init_sync)

    def record(self, log_entry: dict[str, Any], trace: dict[str, Any]) -> None:
        """Store a request summary and trace in the in-memory cache."""
        self._recent.append(log_entry)
        self._details[log_entry["id"]] = trace
        self._prune_memory()

    def list_logs(self) -> list[dict[str, Any]]:
        """Return recent request summaries, newest first."""
        return list(reversed(self._recent))

    async def get_detail(self, log_id: str) -> Optional[dict[str, Any]]:
        """Return a trace from memory or the persisted on-disk copy."""
        detail = self._details.get(log_id)
        if detail is not None:
            return detail
        return await asyncio.to_thread(self._read_detail_sync, log_id)

    async def persist(self, log_entry: dict[str, Any], trace: dict[str, Any]) -> None:
        """Persist a request summary and full trace to disk."""
        async with self._persist_lock:
            await asyncio.to_thread(self._persist_sync, log_entry, trace)

    def _init_sync(self) -> None:
        self._detail_dir.mkdir(parents=True, exist_ok=True)
        self._load_recent_sync()
        self._prune_orphaned_detail_files_sync()

    def _detail_path(self, log_id: str) -> Path:
        return self._detail_dir / f"{log_id}.json"

    def _load_recent_sync(self) -> None:
        if not self._index_path.exists():
            return

        disk_entries: deque[dict[str, Any]] = deque()
        with self._index_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed trace index line %d in %s",
                        line_number,
                        self._index_path,
                    )
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    disk_entries.append(entry)

        self._disk_entries = disk_entries
        self._prune_disk_index_sync()
        self._recent = deque(
            list(self._disk_entries)[-self._max_entries:],
            maxlen=self._max_entries,
        )
        self._prune_memory()

    def _read_detail_sync(self, log_id: str) -> Optional[dict[str, Any]]:
        path = self._detail_path(log_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read persisted trace %s", path)
            return None
        return payload if isinstance(payload, dict) else None

    def _persist_sync(self, log_entry: dict[str, Any], trace: dict[str, Any]) -> None:
        trace_id = log_entry["id"]
        detail_path = self._detail_path(trace_id)
        sanitized_trace = self._sanitize_trace_for_disk(trace)
        index_exists = self._index_path.exists()

        detail_path.write_text(
            json.dumps(sanitized_trace, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        self._disk_entries.append(log_entry)
        pruned_ids = self._prune_disk_index_sync()
        if pruned_ids or not index_exists:
            for trace_id in pruned_ids:
                self._detail_path(trace_id).unlink(missing_ok=True)
        else:
            with self._index_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_entry, separators=(",", ":")))
                handle.write("\n")

    def _prune_memory(self) -> None:
        valid_ids = {entry["id"] for entry in self._recent}
        for detail_id in list(self._details):
            if detail_id not in valid_ids:
                self._details.pop(detail_id, None)

    def _prune_disk_index_sync(self) -> list[str]:
        pruned_ids: list[str] = []
        while len(self._disk_entries) > self._max_disk_entries:
            oldest = self._disk_entries.popleft()
            trace_id = oldest.get("id")
            if trace_id:
                pruned_ids.append(trace_id)
        if pruned_ids or not self._index_path.exists():
            self._rewrite_index_sync()
        return pruned_ids

    def _rewrite_index_sync(self) -> None:
        with self._index_path.open("w", encoding="utf-8") as handle:
            for entry in self._disk_entries:
                handle.write(json.dumps(entry, separators=(",", ":")))
                handle.write("\n")

    def _prune_orphaned_detail_files_sync(self) -> None:
        valid_ids = {entry.get("id") for entry in self._disk_entries if entry.get("id")}
        for path in self._detail_dir.glob("*.json"):
            if path.stem not in valid_ids:
                path.unlink(missing_ok=True)

    def _sanitize_trace_for_disk(self, trace: dict[str, Any]) -> dict[str, Any]:
        sanitized = copy.deepcopy(trace)
        incoming = sanitized.get("incoming")
        if isinstance(incoming, dict):
            self._trim_body(incoming.get("body"))

        attempts = sanitized.get("attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                request = attempt.get("request")
                if isinstance(request, dict):
                    self._trim_body(request.get("body"))
                response = attempt.get("response")
                if isinstance(response, dict):
                    self._trim_body(response.get("body"))
        return sanitized

    def _trim_body(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        text = body.get("text")
        if not isinstance(text, str):
            return
        if len(text) <= self._max_body_chars:
            return
        body["text"] = text[: self._max_body_chars]
        body["text_truncated"] = True
        body["text_total_chars"] = len(text)
