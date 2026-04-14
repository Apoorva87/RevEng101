"""Tests for server-side provider probes and rate-limit snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from oauthrouter.models import AppConfig, ProviderConfig, ServerConfig, Token
from oauthrouter.server import (
    OPENAI_USAGE_URL,
    _openai_usage_snapshot,
    _probe_request_for_token,
    _probe_snippet,
    _rate_limit_snapshot_from_headers,
    api_test_provider,
    token_rate_limits,
)
from oauthrouter.token_manager import TokenManager
from oauthrouter.token_store import TokenStore


@pytest.fixture
def config():
    return AppConfig(
        server=ServerConfig(),
        providers={
            "claude": ProviderConfig(
                upstream="https://api.anthropic.com",
                auth_header="Authorization",
                auth_prefix="Bearer",
                extra_headers={"anthropic-beta": "oauth-2025-04-20"},
            ),
            "openai": ProviderConfig(
                upstream="https://api.openai.com",
                auth_header="Authorization",
                auth_prefix="Bearer",
            ),
        },
    )


@pytest.fixture
async def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TokenStore(path)
    await s.init_db()
    yield s
    await s.close()
    os.unlink(path)


@pytest.fixture(autouse=True)
def clear_rate_limits():
    token_rate_limits.clear()
    yield
    token_rate_limits.clear()


def _request_with_app(app, body: dict | None = None) -> Request:
    payload = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/providers/openai/test",
        "headers": [(b"content-type", b"application/json")],
        "app": app,
    }
    return Request(scope, receive)


class StubAsyncClient:
    """Minimal async client stub for endpoint tests."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def test_probe_request_for_openai_uses_usage_endpoint(config: AppConfig):
    token = Token(
        id="codex",
        provider="openai",
        access_token="token-123",
        account_id="user-123",
    )

    method, url, headers, body = _probe_request_for_token(
        "openai",
        config.providers["openai"],
        token,
    )

    assert method == "GET"
    assert url == OPENAI_USAGE_URL
    assert headers["Authorization"] == "Bearer token-123"
    assert headers["ChatGPT-Account-Id"] == "user-123"
    assert body is None


def test_probe_request_for_claude_adds_required_headers(config: AppConfig):
    token = Token(id="claude", provider="claude", access_token="token-abc")

    method, url, headers, body = _probe_request_for_token(
        "claude",
        config.providers["claude"],
        token,
    )

    assert method == "POST"
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["Authorization"] == "Bearer token-abc"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert body["model"] == "claude-haiku-4-5-20251001"


def test_rate_limit_snapshot_parses_anthropic_windows():
    snapshot = _rate_limit_snapshot_from_headers(
        {
            "anthropic-ratelimit-unified-status": "ok",
            "anthropic-ratelimit-unified-5h-utilization": "0.25",
            "anthropic-ratelimit-unified-5h-status": "ok",
            "anthropic-ratelimit-unified-5h-reset": "2026-04-14T00:00:00Z",
            "anthropic-ratelimit-unified-7d-utilization": "0.8",
            "anthropic-ratelimit-unified-7d-status": "warn",
            "anthropic-ratelimit-unified-7d-reset": "2026-04-20T00:00:00Z",
        }
    )

    assert snapshot is not None
    assert snapshot["overall_status"] == "ok"
    assert snapshot["5h_utilization"] == pytest.approx(0.25)
    assert snapshot["7d_utilization"] == pytest.approx(0.8)
    assert [window["label"] for window in snapshot["windows"]] == ["5h", "7d"]


def test_rate_limit_snapshot_parses_generic_windows():
    snapshot = _rate_limit_snapshot_from_headers(
        {
            "x-ratelimit-limit-requests-5d": "100",
            "x-ratelimit-remaining-requests-5d": "40",
            "x-ratelimit-status-5d": "ok",
            "x-ratelimit-reset-requests-5d": "2026-04-18T00:00:00Z",
            "x-ratelimit-utilization-7day": "75",
            "x-ratelimit-status-7day": "warn",
            "x-ratelimit-reset-requests-7day": "2026-04-20T00:00:00Z",
        }
    )

    assert snapshot is not None
    assert snapshot["5d_utilization"] == pytest.approx(0.6)
    assert snapshot["7d_utilization"] == pytest.approx(0.75)
    assert [window["label"] for window in snapshot["windows"]] == ["5d", "7d"]


def test_openai_usage_snapshot_parses_wham_usage():
    snapshot = _openai_usage_snapshot(
        {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": 18000,
                    "reset_at": 1776113114,
                },
                "secondary_window": {
                    "used_percent": 15,
                    "limit_window_seconds": 604800,
                    "reset_at": 1776666035,
                },
            },
        }
    )

    assert snapshot is not None
    assert snapshot["plan_type"] == "plus"
    assert snapshot["overall_status"] == "ok"
    assert snapshot["5h_utilization"] == pytest.approx(0.03)
    assert snapshot["7d_utilization"] == pytest.approx(0.15)
    assert [window["label"] for window in snapshot["windows"]] == ["5h", "7d"]


def test_probe_snippet_handles_openai_array_content():
    snippet = _probe_snippet(
        "openai",
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "OK"},
                            {"type": "text", "text": " done"},
                        ]
                    }
                }
            ]
        },
        True,
    )
    assert snippet == "OK done"


@pytest.mark.asyncio
async def test_api_test_provider_returns_rate_limits_for_openai(
    store: TokenStore,
    config: AppConfig,
):
    token = Token(
        id="codex",
        provider="openai",
        access_token="token-123",
        account_id="user-yiPxy0GUC9C0xI1CN82AuyKu",
    )
    await store.add_token(token)

    response = httpx.Response(
        200,
        json={
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": 18000,
                    "reset_at": 1776113114,
                },
                "secondary_window": {
                    "used_percent": 15,
                    "limit_window_seconds": 604800,
                    "reset_at": 1776666035,
                },
            },
        },
    )
    http_client = StubAsyncClient(response)
    token_manager = TokenManager(store, http_client, config)
    app = SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            store=store,
            token_manager=token_manager,
            http_client=http_client,
        )
    )

    request = _request_with_app(app, {"token_id": "codex"})
    result = await api_test_provider("openai", request)
    payload = json.loads(result.body)

    assert result.status_code == 200
    assert payload["ok"] is True
    assert payload["token_id"] == "codex"
    assert payload["test_url"] == OPENAI_USAGE_URL
    assert payload["snippet"] == "Plan plus · 5h 3% · 7d 15%"
    assert payload["rate_limits"]["5h_utilization"] == pytest.approx(0.03)
    assert payload["rate_limits"]["7d_utilization"] == pytest.approx(0.15)
    assert http_client.calls[0]["method"] == "GET"
    assert http_client.calls[0]["url"] == OPENAI_USAGE_URL
    assert http_client.calls[0]["headers"]["ChatGPT-Account-Id"] == "user-yiPxy0GUC9C0xI1CN82AuyKu"


@pytest.mark.asyncio
async def test_api_test_provider_allows_selected_unhealthy_token_and_recovers_it(
    store: TokenStore,
    config: AppConfig,
):
    token = Token(
        id="codex",
        provider="openai",
        access_token="token-123",
        account_id="user-123",
        status="unhealthy",
    )
    await store.add_token(token)

    response = httpx.Response(
        200,
        json={
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": 18000,
                    "reset_at": 1776113114,
                },
                "secondary_window": {
                    "used_percent": 15,
                    "limit_window_seconds": 604800,
                    "reset_at": 1776666035,
                },
            },
        },
    )
    http_client = StubAsyncClient(response)
    token_manager = TokenManager(store, http_client, config)
    app = SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            store=store,
            token_manager=token_manager,
            http_client=http_client,
        )
    )

    request = _request_with_app(app, {"token_id": "codex"})
    result = await api_test_provider("openai", request)
    payload = json.loads(result.body)
    refreshed = await store.get_token("codex")

    assert result.status_code == 200
    assert payload["ok"] is True
    assert refreshed is not None
    assert refreshed.status.value == "healthy"
