"""Stateful token rate-limit snapshot storage."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from oauthrouter.rate_limits import (
    update_token_rate_limits_from_headers,
    update_token_rate_limits_from_probe,
)


class RateLimitStore:
    """In-memory rate-limit snapshots keyed by token ID."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self._snapshots.clear()

    @property
    def snapshots(self) -> dict[str, dict[str, Any]]:
        return self._snapshots

    def get(self, token_id: str) -> Optional[dict[str, Any]]:
        return self._snapshots.get(token_id)

    def rename_token(self, old_id: str, new_id: str) -> None:
        if old_id in self._snapshots:
            self._snapshots[new_id] = self._snapshots.pop(old_id)

    def update_from_headers(
        self,
        token_id: str,
        headers: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        return update_token_rate_limits_from_headers(
            self._snapshots,
            token_id,
            headers,
        )

    def update_from_probe(
        self,
        token_id: str,
        provider_name: str,
        headers: dict[str, Any],
        body: Any,
    ) -> Optional[dict[str, Any]]:
        return update_token_rate_limits_from_probe(
            self._snapshots,
            token_id,
            provider_name,
            headers,
            body,
        )

    def update_from_attempts(self, attempts: Iterable[dict[str, Any]]) -> None:
        for attempt in attempts:
            token_id = attempt.get("token_id")
            response = attempt.get("response") or {}
            headers = response.get("headers") or {}
            if not token_id or not headers:
                continue
            self.update_from_headers(token_id, headers)
