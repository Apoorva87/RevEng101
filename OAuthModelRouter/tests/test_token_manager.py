"""Tests for the token manager."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from oauthrouter.models import Token, TokenStatus
from oauthrouter.token_manager import (
    NoHealthyTokensError,
    NoUsableTokensError,
    TokenManager,
)
from oauthrouter.token_store import TokenStore


@pytest.fixture
async def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TokenStore(path)
    await s.init_db()
    yield s
    await s.close()
    os.unlink(path)


def _make_token(name="t1", provider="claude", **kwargs):
    defaults = dict(
        id=name,
        provider=provider,
        access_token=f"at-{name}",
    )
    defaults.update(kwargs)
    return Token(**defaults)


@pytest.mark.asyncio
async def test_pick_token_basic(store: TokenStore):
    """pick_token returns a healthy token and marks it as used."""
    await store.add_token(_make_token("t1", "claude"))
    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    token = await manager.pick_token("claude")
    assert token.id == "t1"

    # Verify it was marked as used
    updated = await store.get_token("t1")
    assert updated.last_used_at is not None
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_uses_priority_not_lru(store: TokenStore):
    """pick_token does not rotate between equal-priority tokens by LRU."""
    await store.add_token(_make_token("t1", "claude"))
    await store.add_token(_make_token("t2", "claude"))
    await store.mark_used("t1")

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    token = await manager.pick_token("claude")
    assert token.id == "t1"
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_prefers_lower_priority(store: TokenStore):
    """pick_token selects explicit defaults before normal LRU tokens."""
    await store.add_token(_make_token("keychain", "claude", priority=100))
    await store.add_token(_make_token("long-lived", "claude", priority=0))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    token = await manager.pick_token("claude")
    assert token.id == "long-lived"
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_no_healthy(store: TokenStore):
    """pick_token raises NoHealthyTokensError when all tokens are unhealthy."""
    await store.add_token(_make_token("t1", "claude"))
    await store.mark_unhealthy("t1")

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    with pytest.raises(NoHealthyTokensError) as exc_info:
        await manager.pick_token("claude")
    assert exc_info.value.provider == "claude"
    assert exc_info.value.total_tokens == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_no_tokens_at_all(store: TokenStore):
    """pick_token raises NoHealthyTokensError when provider has no tokens."""
    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    with pytest.raises(NoHealthyTokensError):
        await manager.pick_token("claude")
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_skips_rate_limited_tokens(store: TokenStore):
    """pick_token avoids tokens in temporary 429 cooldown."""
    await store.add_token(_make_token("t1", "claude"))
    await store.add_token(_make_token("t2", "claude"))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)
    manager.mark_token_rate_limited(
        "t1",
        retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    token = await manager.pick_token("claude")
    assert token.id == "t2"
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_all_rate_limited_raises_no_usable(store: TokenStore):
    """pick_token reports when all healthy tokens are cooling down."""
    await store.add_token(_make_token("t1", "claude"))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    manager.mark_token_rate_limited("t1", retry_at=retry_at)

    with pytest.raises(NoUsableTokensError) as exc_info:
        await manager.pick_token("claude")
    assert exc_info.value.provider == "claude"
    assert exc_info.value.cooling_tokens == 1
    assert exc_info.value.next_retry_at == retry_at
    await http.aclose()


@pytest.mark.asyncio
async def test_pick_token_expired_refreshes(store: TokenStore):
    """pick_token refreshes an expired token before returning it."""
    expired_at = datetime.utcnow() - timedelta(hours=1)
    await store.add_token(
        _make_token(
            "t1",
            "claude",
            expires_at=expired_at,
            refresh_token="rt-123",
            token_endpoint="https://example.com/token",
        )
    )

    # Mock the HTTP client to return a successful refresh
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new-at-123",
        "refresh_token": "new-rt-123",
        "expires_in": 3600,
    }

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=mock_response)
    manager = TokenManager(store, http)

    token = await manager.pick_token("claude")
    assert token.access_token == "new-at-123"
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_token_success(store: TokenStore):
    """refresh_token updates the store on success."""
    await store.add_token(
        _make_token(
            "t1",
            "claude",
            refresh_token="rt-123",
            token_endpoint="https://example.com/token",
        )
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new-at",
        "expires_in": 7200,
    }

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=mock_response)
    manager = TokenManager(store, http)

    token = await store.get_token("t1")
    refreshed = await manager.refresh_token(token)

    assert refreshed is not None
    assert refreshed.access_token == "new-at"
    assert refreshed.expires_at is not None


@pytest.mark.asyncio
async def test_refresh_token_no_refresh_token(store: TokenStore):
    """refresh_token returns None when token has no refresh_token."""
    await store.add_token(_make_token("t1", "claude"))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    token = await store.get_token("t1")
    result = await manager.refresh_token(token)
    assert result is None
    await http.aclose()


@pytest.mark.asyncio
async def test_refresh_token_http_failure(store: TokenStore):
    """refresh_token returns None on HTTP error."""
    await store.add_token(
        _make_token(
            "t1",
            "claude",
            refresh_token="rt-123",
            token_endpoint="https://example.com/token",
        )
    )

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "bad request"

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=mock_response)
    manager = TokenManager(store, http)

    token = await store.get_token("t1")
    result = await manager.refresh_token(token)
    assert result is None


@pytest.mark.asyncio
async def test_handle_auth_failure_with_failover(store: TokenStore):
    """handle_auth_failure marks the failed token unhealthy and returns the next one."""
    await store.add_token(_make_token("t1", "claude"))
    await store.add_token(_make_token("t2", "claude"))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    t1 = await store.get_token("t1")
    next_token = await manager.handle_auth_failure(t1, "claude")

    assert next_token is not None
    assert next_token.id == "t2"

    # t1 should now be unhealthy
    t1_updated = await store.get_token("t1")
    assert t1_updated.status == TokenStatus.UNHEALTHY
    await http.aclose()


@pytest.mark.asyncio
async def test_handle_auth_failure_all_exhausted(store: TokenStore):
    """handle_auth_failure returns None when no tokens remain."""
    await store.add_token(_make_token("t1", "claude"))

    http = httpx.AsyncClient()
    manager = TokenManager(store, http)

    t1 = await store.get_token("t1")
    result = await manager.handle_auth_failure(t1, "claude")

    assert result is None
    await http.aclose()
