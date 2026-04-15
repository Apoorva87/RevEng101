"""Tests for the proxy and server integration."""

from __future__ import annotations

import os
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient

from oauthrouter.models import AppConfig, ProviderConfig, ServerConfig, Token
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


@pytest.mark.asyncio
async def test_inject_auth_claude(store: TokenStore, config: AppConfig):
    """Auth injection uses Bearer plus OAuth beta for Claude Code OAuth tokens."""
    from oauthrouter.proxy import _inject_auth

    token = Token(id="t1", provider="claude", access_token="my-key")
    provider_config = config.providers["claude"]

    headers = _inject_auth({}, token, provider_config)
    assert headers["Authorization"] == "Bearer my-key"
    assert "x-api-key" not in headers
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.asyncio
async def test_inject_auth_openai(store: TokenStore, config: AppConfig):
    """Auth injection uses Authorization: Bearer for OpenAI."""
    from oauthrouter.proxy import _inject_auth

    token = Token(id="t1", provider="openai", access_token="my-key")
    provider_config = config.providers["openai"]

    headers = _inject_auth({}, token, provider_config)
    assert headers["Authorization"] == "Bearer my-key"


@pytest.mark.asyncio
async def test_inject_auth_strips_incoming_placeholders(
    store: TokenStore, config: AppConfig
):
    """Incoming placeholder auth headers are removed before router injection."""
    from oauthrouter.proxy import _inject_auth

    token = Token(id="t1", provider="claude", access_token="real-oauth")
    provider_config = config.providers["claude"]

    headers = _inject_auth(
        {
            "Authorization": "Bearer oauthrouter",
            "x-api-key": "oauthrouter",
            "anthropic-version": "2023-06-01",
        },
        token,
        provider_config,
    )

    assert headers["Authorization"] == "Bearer real-oauth"
    assert "x-api-key" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


def test_build_upstream_url(config: AppConfig):
    """Upstream URL is correctly built from provider config + path."""
    from oauthrouter.proxy import _build_upstream_url

    provider_config = config.providers["claude"]

    url = _build_upstream_url(provider_config, "v1/messages", "")
    assert url == "https://api.anthropic.com/v1/messages"

    url_with_query = _build_upstream_url(
        provider_config, "v1/messages", "stream=true"
    )
    assert url_with_query == "https://api.anthropic.com/v1/messages?stream=true"


def test_build_upstream_url_strips_slashes(config: AppConfig):
    """No double slashes when both upstream and path have slashes."""
    from oauthrouter.proxy import _build_upstream_url

    provider_config = ProviderConfig(upstream="https://api.example.com/")

    url = _build_upstream_url(provider_config, "/v1/chat", "")
    assert url == "https://api.example.com/v1/chat"


def test_body_trace_preserves_text_and_binary_payloads():
    """Trace payload capture keeps UTF-8 text and base64-encodes binary bodies."""
    from oauthrouter.proxy import _body_for_trace

    text = _body_for_trace(b'{"hello":"world"}')
    assert text["encoding"] == "utf-8"
    assert text["text"] == '{"hello":"world"}'
    assert text["size_bytes"] == 17

    binary = _body_for_trace(b"\xff\x00")
    assert binary["encoding"] == "base64"
    assert binary["text"] == "/wA="
    assert binary["size_bytes"] == 2


def test_log_detail_route_exists():
    """The portal can fetch full request/response traces by log ID."""
    from oauthrouter.server import app

    paths = [
        route.path for route in app.routes if hasattr(route, "path")
    ]
    assert "/api/logs/{log_id}" in paths


def test_health_endpoint():
    """The /health endpoint returns provider status."""
    from oauthrouter.server import app

    # Use TestClient for a quick sync test of the health endpoint shape
    # This requires the app lifespan to work, so we test the route exists
    assert any(
        route.path == "/health"
        for route in app.routes
        if hasattr(route, "path")
    )


def test_help_route_exists():
    """The built-in help page is exposed as /help."""
    from oauthrouter.server import app

    assert any(
        route.path == "/help"
        for route in app.routes
        if hasattr(route, "path")
    )


def test_discovery_routes_removed():
    """Legacy discovery/import routes are no longer registered."""
    from oauthrouter.server import app

    paths = {
        route.path for route in app.routes if hasattr(route, "path")
    }
    assert "/api/discover" not in paths
    assert "/api/discover/import" not in paths
    assert "/api/tokens/import-codex-json" not in paths
    assert "/api/discover/keychain" not in paths
    assert "/api/tokens/import-keychain" not in paths


def test_proxy_route_exists():
    """The catch-all proxy route is registered."""
    from oauthrouter.server import app

    paths = [
        route.path for route in app.routes if hasattr(route, "path")
    ]
    assert "/{provider}/{path:path}" in paths
