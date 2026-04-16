"""Token selection, refresh, and failover logic for OAuthModelRouter."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from oauthrouter.models import AppConfig, Token, TokenStatus, is_token_expired
from oauthrouter.token_store import TokenStore

logger = logging.getLogger(__name__)


class NoHealthyTokensError(Exception):
    """Raised when no healthy tokens are available for a provider."""

    def __init__(self, provider: str, total_tokens: int) -> None:
        self.provider = provider
        self.total_tokens = total_tokens
        super().__init__(
            f"No healthy tokens available for provider '{provider}' "
            f"({total_tokens} total, all unhealthy)"
        )


class NoUsableTokensError(Exception):
    """Raised when healthy tokens exist but none can currently be used."""

    def __init__(
        self,
        provider: str,
        total_tokens: int,
        healthy_tokens: int,
        cooling_tokens: int,
        excluded_tokens: int = 0,
        next_retry_at: Optional[datetime] = None,
    ) -> None:
        self.provider = provider
        self.total_tokens = total_tokens
        self.healthy_tokens = healthy_tokens
        self.cooling_tokens = cooling_tokens
        self.excluded_tokens = excluded_tokens
        self.next_retry_at = next_retry_at

        parts = [f"{healthy_tokens} healthy"]
        if cooling_tokens:
            parts.append(f"{cooling_tokens} cooling down")
        if excluded_tokens:
            parts.append(f"{excluded_tokens} excluded")

        message = (
            f"No usable tokens available for provider '{provider}' "
            f"({total_tokens} total, {', '.join(parts)})"
        )
        if next_retry_at is not None:
            message = f"{message}; next retry at {next_retry_at.isoformat()}"
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TokenManager:
    """Manages token selection, refresh, and failover.

    Uses explicit token priority for selection, skips tokens in temporary
    rate-limit cooldown, and serializes refresh attempts with per-token locks.
    """

    def __init__(
        self,
        store: TokenStore,
        http_client: httpx.AsyncClient,
        config: Optional[AppConfig] = None,
    ) -> None:
        self._store = store
        self._http = http_client
        self._config = config
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._rate_limit_cooldowns: dict[str, datetime] = {}

    def _get_lock(self, token_id: str) -> asyncio.Lock:
        """Get or create a per-token lock for refresh serialization."""
        if token_id not in self._refresh_locks:
            self._refresh_locks[token_id] = asyncio.Lock()
        return self._refresh_locks[token_id]

    def _prune_rate_limit_cooldowns(self) -> None:
        """Drop any expired in-memory cooldown entries."""
        now = _utcnow()
        for token_id, retry_at in list(self._rate_limit_cooldowns.items()):
            if _normalize_utc(retry_at) <= now:
                self._rate_limit_cooldowns.pop(token_id, None)

    def get_rate_limit_cooldown(self, token_id: str) -> Optional[datetime]:
        """Return the cooldown-until timestamp for a token, if any."""
        self._prune_rate_limit_cooldowns()
        retry_at = self._rate_limit_cooldowns.get(token_id)
        if retry_at is None:
            return None
        return _normalize_utc(retry_at)

    def mark_token_rate_limited(
        self,
        token_id: str,
        *,
        retry_at: Optional[datetime] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> datetime:
        """Temporarily remove a token from selection after a 429."""
        if retry_at is None:
            delay_seconds = max(1, int(retry_after_seconds or 60))
            retry_at = _utcnow() + timedelta(seconds=delay_seconds)
        retry_at = _normalize_utc(retry_at)

        current = self._rate_limit_cooldowns.get(token_id)
        if current is not None:
            current = _normalize_utc(current)
            if current > retry_at:
                retry_at = current

        self._rate_limit_cooldowns[token_id] = retry_at
        logger.warning(
            "Token %s entered rate-limit cooldown until %s",
            token_id,
            retry_at.isoformat(),
        )
        return retry_at

    async def pick_token(
        self,
        provider: str,
        *,
        exclude_token_ids: Optional[set[str]] = None,
    ) -> Token:
        """Select the best available token for a provider.

        Strategy: explicit priority ordering among healthy tokens, skipping
        tokens that are in a temporary rate-limit cooldown. If the selected
        token is expired, attempt a refresh before returning it.

        Raises NoHealthyTokensError if no healthy tokens exist, and
        NoUsableTokensError if healthy tokens exist but are all cooling down
        or excluded for this selection attempt.
        """
        request_id = uuid.uuid4().hex[:8]
        logger.debug("[%s] Picking token for provider=%s", request_id, provider)

        healthy = await self._store.get_healthy_tokens(provider)
        if not healthy:
            all_tokens = await self._store.list_tokens(provider)
            logger.error(
                "[%s] No healthy tokens for provider=%s (total=%d). "
                "Unhealthy tokens: %s",
                request_id,
                provider,
                len(all_tokens),
                [t.id for t in all_tokens],
            )
            raise NoHealthyTokensError(provider, len(all_tokens))

        self._prune_rate_limit_cooldowns()
        excluded = exclude_token_ids or set()
        candidates: list[Token] = []
        cooling_tokens = 0
        next_retry_at: Optional[datetime] = None
        excluded_tokens = 0

        for candidate in healthy:
            if candidate.id in excluded:
                excluded_tokens += 1
                continue

            cooldown_until = self.get_rate_limit_cooldown(candidate.id)
            if cooldown_until is not None:
                cooling_tokens += 1
                if next_retry_at is None or cooldown_until < next_retry_at:
                    next_retry_at = cooldown_until
                continue

            candidates.append(candidate)

        if not candidates:
            total_tokens = len(await self._store.list_tokens(provider))
            logger.warning(
                "[%s] No usable tokens for provider=%s "
                "(healthy=%d cooling=%d excluded=%d next_retry_at=%s)",
                request_id,
                provider,
                len(healthy),
                cooling_tokens,
                excluded_tokens,
                next_retry_at.isoformat() if next_retry_at else "unknown",
            )
            raise NoUsableTokensError(
                provider=provider,
                total_tokens=total_tokens,
                healthy_tokens=len(healthy),
                cooling_tokens=cooling_tokens,
                excluded_tokens=excluded_tokens,
                next_retry_at=next_retry_at,
            )

        token = candidates[0]
        logger.debug(
            "[%s] Selected token=%s for provider=%s (of %d healthy)",
            request_id,
            token.id,
            provider,
            len(healthy),
        )

        # If the token is expired, try to refresh it before use
        if is_token_expired(token):
            logger.info(
                "[%s] Token %s is expired (expires_at=%s), attempting refresh",
                request_id,
                token.id,
                token.expires_at,
            )
            refreshed = await self.refresh_token(token)
            if refreshed:
                token = refreshed
            else:
                # Refresh failed — mark unhealthy, try next token (non-recursive)
                logger.warning(
                    "[%s] Refresh failed for %s, marking unhealthy and trying next",
                    request_id,
                    token.id,
                )
                await self._store.mark_unhealthy(token.id)
                merged_excludes = (exclude_token_ids or set()) | {token.id}
                return await self.pick_token(
                    provider,
                    exclude_token_ids=merged_excludes,
                )

        await self._store.mark_used(token.id)
        return token

    async def handle_auth_failure(
        self, token: Token, provider: str
    ) -> Optional[Token]:
        """Handle a 401/403 from upstream.

        Attempts to refresh the failed token. If refresh succeeds, returns
        the refreshed token. If refresh fails, marks it unhealthy and returns
        the next healthy token (or None if none remain).
        """
        request_id = uuid.uuid4().hex[:8]
        logger.warning(
            "[%s] Auth failure for token=%s provider=%s, attempting recovery",
            request_id,
            token.id,
            provider,
        )

        refreshed = await self.refresh_token(token)
        if refreshed:
            logger.info(
                "[%s] Token %s refreshed successfully after auth failure",
                request_id,
                token.id,
            )
            await self._store.mark_used(refreshed.id)
            return refreshed

        # Refresh failed — mark this token as unhealthy
        logger.warning(
            "[%s] Refresh failed for %s, marking unhealthy",
            request_id,
            token.id,
        )
        await self._store.mark_unhealthy(token.id)

        # Try to find the next healthy token
        try:
            next_token = await self.pick_token(
                provider,
                exclude_token_ids={token.id},
            )
            logger.info(
                "[%s] Failing over from %s to %s",
                request_id,
                token.id,
                next_token.id,
            )
            return next_token
        except (NoHealthyTokensError, NoUsableTokensError):
            logger.error(
                "[%s] No more healthy tokens for provider=%s after %s failed",
                request_id,
                provider,
                token.id,
            )
            return None

    async def mark_token_unhealthy(self, token_id: str) -> None:
        """Remove a token from rotation."""
        await self._store.mark_unhealthy(token_id)

    def _resolve_token_endpoint(self, token: Token) -> Optional[str]:
        """Resolve the token refresh endpoint.

        Priority: token-level > provider-level config > None.
        """
        if token.token_endpoint:
            return token.token_endpoint
        if self._config:
            provider_cfg = self._config.providers.get(token.provider)
            if provider_cfg and provider_cfg.token_endpoint:
                return provider_cfg.token_endpoint
        return None

    def _resolve_client_id(self, token: Token) -> Optional[str]:
        """Resolve the OAuth client_id for refresh requests.

        Priority: token-level > provider-level config > None.
        """
        if token.oauth_client_id:
            return token.oauth_client_id
        if self._config:
            provider_cfg = self._config.providers.get(token.provider)
            if provider_cfg and provider_cfg.oauth_client_id:
                return provider_cfg.oauth_client_id
        return None

    async def refresh_token(self, token: Token) -> Optional[Token]:
        """Attempt to refresh an OAuth token using its refresh_token.

        Uses a per-token lock to prevent concurrent refresh attempts.
        Falls back to provider-level token_endpoint and oauth_client_id from config.
        Returns the updated Token on success, None on failure.
        """
        token_endpoint = self._resolve_token_endpoint(token)

        if not token.refresh_token or not token_endpoint:
            logger.debug(
                "Cannot refresh token %s: refresh_token=%s, token_endpoint=%s",
                token.id,
                "present" if token.refresh_token else "missing",
                token_endpoint or "missing",
            )
            return None

        lock = self._get_lock(token.id)
        async with lock:
            # Re-check the token state — another coroutine may have refreshed it
            current = await self._store.get_token(token.id)
            if current and current.access_token != token.access_token:
                logger.debug(
                    "Token %s was already refreshed by another request, using updated version",
                    token.id,
                )
                return current

            client_id = self._resolve_client_id(token)
            logger.info(
                "Refreshing token %s via %s (client_id=%s)",
                token.id,
                token_endpoint,
                client_id[:20] + "..." if client_id and len(client_id) > 20 else client_id,
            )

            post_data = {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            }
            if client_id:
                post_data["client_id"] = client_id
            if token.scopes:
                post_data["scope"] = token.scopes

            try:
                response = await self._http.post(
                    token_endpoint,
                    data=post_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "User-Agent": "oauthrouter/0.1.0",
                        "Accept": "application/json",
                    },
                    timeout=30.0,
                )

                if response.status_code != 200:
                    logger.warning(
                        "Token refresh failed for %s: HTTP %d — %s",
                        token.id,
                        response.status_code,
                        response.text[:200],
                    )
                    return None

                data = response.json()
                new_access = data.get("access_token")
                if not new_access:
                    logger.warning(
                        "Token refresh response for %s missing access_token: %s",
                        token.id,
                        list(data.keys()),
                    )
                    return None

                new_refresh = data.get("refresh_token", token.refresh_token)
                expires_in = data.get("expires_in")
                new_expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    if expires_in
                    else None
                )

                await self._store.update_token(
                    token.id,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    expires_at=new_expires_at,
                    status=TokenStatus.HEALTHY,
                )

                logger.info(
                    "Token %s refreshed successfully. expires_in=%s",
                    token.id,
                    expires_in,
                )

                return await self._store.get_token(token.id)

            except httpx.HTTPError as exc:
                logger.error(
                    "Token refresh HTTP error for %s: %s",
                    token.id,
                    exc,
                )
                return None

