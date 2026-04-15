"""Health and catch-all proxy routes."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask, BackgroundTasks

from oauthrouter.app_state import get_app_services
from oauthrouter.proxy import forward_request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Health check endpoint showing token status per provider."""
    services = get_app_services(request)
    providers_health = {}
    for provider_name in services.config.providers:
        all_tokens = await services.store.list_tokens(provider_name)
        healthy = [token for token in all_tokens if token.status.value == "healthy"]
        providers_health[provider_name] = {
            "healthy_tokens": len(healthy),
            "total_tokens": len(all_tokens),
            "token_names": [
                {"name": token.id, "status": token.status.value}
                for token in all_tokens
            ],
        }

    overall = (
        all(info["healthy_tokens"] > 0 for info in providers_health.values())
        if providers_health
        else False
    )
    return JSONResponse(
        {"status": "ok" if overall else "degraded", "providers": providers_health}
    )


@router.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def api_not_found(path: str) -> JSONResponse:
    """Return a clean 404 for unknown API paths before proxy catch-all matching."""
    normalized = f"/api/{path}".rstrip("/") if path else "/api"
    return JSONResponse(
        {"error": f"API endpoint '{normalized}' not found"},
        status_code=404,
    )


def _attach_background_task(
    response: Response,
    task_func: Callable,
    *args,
) -> None:
    """Append a background task without overwriting any existing task."""
    if response.background is None:
        response.background = BackgroundTask(task_func, *args)
        return

    tasks = BackgroundTasks()
    tasks.add_task(response.background)
    tasks.add_task(task_func, *args)
    response.background = tasks


@router.api_route(
    "/{provider}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_route(provider: str, path: str, request: Request) -> Response:
    """Catch-all proxy route — must be registered last."""
    services = get_app_services(request)

    request_id = uuid.uuid4().hex[:8]
    start = time.monotonic()
    trace = {
        "id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "warning": (
            "Auth headers are redacted, but trace bodies may contain live "
            "request/response data and are also persisted to disk."
        ),
    }

    response = await forward_request(
        request=request,
        provider=provider,
        path=path,
        config=services.config,
        token_manager=services.token_manager,
        http_client=services.http_client,
        trace=trace,
        rate_limit_snapshots=services.rate_limits.snapshots,
    )

    elapsed_ms = round((time.monotonic() - start) * 1000)
    trace["final"] = {
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "attempts": len(trace.get("attempts", [])),
    }

    attempts = trace.get("attempts", [])
    used_token = attempts[-1].get("token_id") if attempts else None
    log_entry = {
        "id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "path": f"/{provider}/{path}",
        "provider": provider,
        "token_used": used_token,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "client": request.client.host if request.client else "unknown",
        "has_detail": True,
        "attempts": len(attempts),
    }

    services.trace_store.record(log_entry, trace)
    _attach_background_task(response, services.trace_store.persist, log_entry, trace)
    return response
