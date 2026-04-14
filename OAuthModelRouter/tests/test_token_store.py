"""Tests for the token store."""

from __future__ import annotations

import os
import tempfile

import pytest

from oauthrouter.models import Token, TokenStatus
from oauthrouter.token_store import TokenStore


@pytest.fixture
async def store():
    """Create a temporary token store for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TokenStore(path)
    await s.init_db()
    yield s
    await s.close()
    os.unlink(path)


def _make_token(
    name: str = "test-token",
    provider: str = "claude",
    access_token: str = "at-123",
    **kwargs,
) -> Token:
    return Token(id=name, provider=provider, access_token=access_token, **kwargs)


@pytest.mark.asyncio
async def test_add_and_get(store: TokenStore):
    token = _make_token()
    await store.add_token(token)
    result = await store.get_token("test-token")
    assert result is not None
    assert result.id == "test-token"
    assert result.provider == "claude"
    assert result.access_token == "at-123"
    assert result.status == TokenStatus.HEALTHY


@pytest.mark.asyncio
async def test_get_nonexistent(store: TokenStore):
    result = await store.get_token("nope")
    assert result is None


@pytest.mark.asyncio
async def test_list_tokens(store: TokenStore):
    await store.add_token(_make_token("t1", "claude"))
    await store.add_token(_make_token("t2", "openai"))
    await store.add_token(_make_token("t3", "claude"))

    all_tokens = await store.list_tokens()
    assert len(all_tokens) == 3

    claude_tokens = await store.list_tokens("claude")
    assert len(claude_tokens) == 2
    assert all(t.provider == "claude" for t in claude_tokens)

    openai_tokens = await store.list_tokens("openai")
    assert len(openai_tokens) == 1


@pytest.mark.asyncio
async def test_remove_token(store: TokenStore):
    await store.add_token(_make_token())
    assert await store.remove_token("test-token") is True
    assert await store.get_token("test-token") is None
    assert await store.remove_token("test-token") is False


@pytest.mark.asyncio
async def test_mark_unhealthy_and_healthy(store: TokenStore):
    await store.add_token(_make_token())
    await store.mark_unhealthy("test-token")
    token = await store.get_token("test-token")
    assert token.status == TokenStatus.UNHEALTHY

    await store.mark_healthy("test-token")
    token = await store.get_token("test-token")
    assert token.status == TokenStatus.HEALTHY


@pytest.mark.asyncio
async def test_get_healthy_tokens(store: TokenStore):
    await store.add_token(_make_token("healthy1", "claude"))
    await store.add_token(_make_token("healthy2", "claude"))
    await store.add_token(_make_token("sick", "claude"))
    await store.mark_unhealthy("sick")

    healthy = await store.get_healthy_tokens("claude")
    assert len(healthy) == 2
    assert all(t.status == TokenStatus.HEALTHY for t in healthy)


@pytest.mark.asyncio
async def test_get_healthy_tokens_ignores_last_used_for_order(store: TokenStore):
    """Token routing order is explicit priority, not LRU."""
    await store.add_token(_make_token("t1", "claude"))
    await store.add_token(_make_token("t2", "claude"))

    # last_used_at is metadata only; it should not change routing order.
    await store.mark_used("t1")

    healthy = await store.get_healthy_tokens("claude")
    assert healthy[0].id == "t1"
    assert healthy[1].id == "t2"


@pytest.mark.asyncio
async def test_get_healthy_tokens_priority_order(store: TokenStore):
    """Lower-priority values should be selected first."""
    await store.add_token(_make_token("old-keychain", "claude", priority=100))
    await store.add_token(_make_token("long-lived", "claude", priority=0))

    healthy = await store.get_healthy_tokens("claude")
    assert healthy[0].id == "long-lived"
    assert healthy[1].id == "old-keychain"


@pytest.mark.asyncio
async def test_update_token(store: TokenStore):
    await store.add_token(_make_token())
    await store.update_token(
        "test-token",
        access_token="new-at",
        status=TokenStatus.HEALTHY,
    )
    token = await store.get_token("test-token")
    assert token.access_token == "new-at"
    assert token.status == TokenStatus.HEALTHY


@pytest.mark.asyncio
async def test_mark_used(store: TokenStore):
    await store.add_token(_make_token())
    token_before = await store.get_token("test-token")
    assert token_before.last_used_at is None

    await store.mark_used("test-token")
    token_after = await store.get_token("test-token")
    assert token_after.last_used_at is not None
