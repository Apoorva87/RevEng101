"""FastAPI application for OAuthModelRouter."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from oauthrouter.app_state import build_app_services, close_app_services
from oauthrouter.routes.api import router as api_router
from oauthrouter.routes.pages import router as pages_router
from oauthrouter.routes.proxy import router as proxy_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App startup/shutdown: initialize and tear down shared services."""
    services = await build_app_services()
    app.state.services = services

    logger.info(
        "OAuthModelRouter started on %s:%d with providers: %s",
        services.config.server.host,
        services.config.server.port,
        list(services.config.providers.keys()),
    )

    yield

    await close_app_services(services)
    logger.info("OAuthModelRouter shut down")


def create_app() -> FastAPI:
    """Create the FastAPI application with routers in route-match order."""
    app = FastAPI(
        title="OAuthModelRouter",
        description="Local reverse proxy for managing multiple OAuth tokens",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages_router)
    app.include_router(api_router)
    app.include_router(proxy_router)
    return app


app = create_app()
