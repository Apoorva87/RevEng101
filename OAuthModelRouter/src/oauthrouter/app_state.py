"""Application service container for FastAPI request handlers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Request

from oauthrouter.config import DB_PATH, LOG_DIR, load_config
from oauthrouter.models import AppConfig
from oauthrouter.probes import ProbeService
from oauthrouter.rate_limit_store import RateLimitStore
from oauthrouter.token_manager import TokenManager
from oauthrouter.token_store import TokenStore
from oauthrouter.trace_store import TraceStore

MAX_LOG_ENTRIES = 200


@dataclass
class AppServices:
    config: AppConfig
    store: TokenStore
    http_client: httpx.AsyncClient
    token_manager: TokenManager
    trace_store: TraceStore
    rate_limits: RateLimitStore
    probes: ProbeService


async def build_app_services() -> AppServices:
    """Create and initialize the long-lived app services."""
    config = load_config()
    store = TokenStore(str(DB_PATH))
    await store.init_db()

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    token_manager = TokenManager(store, http_client, config)
    trace_store = TraceStore(LOG_DIR, max_entries=MAX_LOG_ENTRIES)
    await trace_store.init()
    rate_limits = RateLimitStore()
    probes = ProbeService(config, store, token_manager, http_client, rate_limits)

    return AppServices(
        config=config,
        store=store,
        http_client=http_client,
        token_manager=token_manager,
        trace_store=trace_store,
        rate_limits=rate_limits,
        probes=probes,
    )


async def close_app_services(services: AppServices) -> None:
    """Close resources owned by the app service container."""
    await services.http_client.aclose()
    await services.store.close()


def get_app_services(request: Request) -> AppServices:
    """Return the initialized service container for a request."""
    return request.app.state.services
