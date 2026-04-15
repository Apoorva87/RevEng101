"""JSON API routes for the dashboard and token management."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from oauthrouter.app_state import get_app_services
from oauthrouter.config import save_config
from oauthrouter.models import ProviderConfig, Token, TokenStatus

router = APIRouter()


def _parse_priority(value, default: int = 100) -> int:
    """Parse a token priority value from JSON."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc


def _string_value(value) -> str:
    """Safely coerce a JSON field to a trimmed string-like value."""
    return value.strip() if isinstance(value, str) else ""


async def _read_json_object(request: Request) -> tuple[dict, Optional[JSONResponse]]:
    """Read a JSON object body, returning a 400 response on invalid input."""
    raw_body = await request.body()
    if not raw_body.strip():
        return {}, None

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return {}, JSONResponse(
            {"error": "Request body must be valid JSON"},
            status_code=400,
        )

    if not isinstance(body, dict):
        return {}, JSONResponse(
            {"error": "Request body must be a JSON object"},
            status_code=400,
        )
    return body, None


def _token_to_dict(token: Token, request: Request) -> dict:
    """Serialize a token for the API, masking sensitive values."""
    services = get_app_services(request)
    now = datetime.now(timezone.utc)
    expires_at_iso = token.expires_at.isoformat() if token.expires_at else None

    is_expired = False
    expires_in_human = "unknown"
    if token.expires_at:
        exp = token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - now
        is_expired = delta.total_seconds() < 0
        secs = int(abs(delta.total_seconds()))
        hours, remainder = divmod(secs, 3600)
        minutes, _ = divmod(remainder, 60)
        if is_expired:
            expires_in_human = f"EXPIRED {hours}h {minutes}m ago"
        elif hours > 0:
            expires_in_human = f"{hours}h {minutes}m"
        else:
            expires_in_human = f"{minutes}m"

    masked = f"***{token.access_token[-8:]}" if len(token.access_token) > 12 else "***"
    payload = {
        "id": token.id,
        "provider": token.provider,
        "status": token.status.value,
        "priority": token.priority,
        "is_expired": is_expired,
        "expires_at": expires_at_iso,
        "expires_in": expires_in_human,
        "has_refresh_token": token.refresh_token is not None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "masked_token": masked,
    }
    rate_limits = services.rate_limits.get(token.id)
    if rate_limits:
        payload["rate_limits"] = rate_limits
    return payload


def _provider_summary(
    name: str,
    provider: ProviderConfig,
    tokens: list[Token],
    request: Request,
) -> dict:
    """Serialize provider config and current token coverage for the UI."""
    services = get_app_services(request)
    healthy_tokens = [token for token in tokens if token.status == TokenStatus.HEALTHY]
    active_tokens = []
    now = datetime.now(timezone.utc)
    for token in healthy_tokens:
        if not token.expires_at:
            active_tokens.append(token)
            continue
        exp = token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp >= now:
            active_tokens.append(token)

    cooling_tokens = []
    for token in healthy_tokens:
        cooldown_until = services.token_manager.get_rate_limit_cooldown(token.id)
        if cooldown_until is not None:
            cooling_tokens.append({"id": token.id, "retry_at": cooldown_until.isoformat()})

    return {
        "name": name,
        "upstream": provider.upstream,
        "auth_header": provider.auth_header,
        "auth_prefix": provider.auth_prefix,
        "token_endpoint": provider.token_endpoint,
        "oauth_client_id": provider.oauth_client_id,
        "extra_headers": provider.extra_headers or {},
        "healthy_tokens": len(healthy_tokens),
        "active_tokens": len(active_tokens),
        "total_tokens": len(tokens),
        "cooling_tokens": cooling_tokens,
        "tokens": [
            {
                "id": token.id,
                "status": token.status.value,
                "is_expired": _token_to_dict(token, request)["is_expired"],
                "priority": token.priority,
            }
            for token in tokens
        ],
    }


@router.get("/api/tokens")
async def api_list_tokens(request: Request) -> JSONResponse:
    """List all tokens with status info."""
    services = get_app_services(request)
    tokens = await services.store.list_tokens()
    return JSONResponse([_token_to_dict(token, request) for token in tokens])


@router.get("/api/providers")
async def api_list_providers(request: Request) -> JSONResponse:
    """List provider configuration and current token coverage."""
    services = get_app_services(request)
    providers = []
    for name, provider in services.config.providers.items():
        tokens = await services.store.list_tokens(name)
        providers.append(_provider_summary(name, provider, tokens, request))
    return JSONResponse(providers)


@router.patch("/api/providers/{provider_name}")
async def api_update_provider(provider_name: str, request: Request) -> JSONResponse:
    """Update provider-level defaults and persist them to config.toml."""
    services = get_app_services(request)
    provider = services.config.providers.get(provider_name)
    if not provider:
        return JSONResponse({"error": f"Provider '{provider_name}' not found"}, status_code=404)

    body, error = await _read_json_object(request)
    if error:
        return error

    merged = provider.model_dump()

    for field in ("upstream", "auth_header"):
        if field in body:
            value = _string_value(body.get(field))
            if not value:
                return JSONResponse({"error": f"{field} is required"}, status_code=400)
            merged[field] = value

    for field in ("auth_prefix", "token_endpoint", "oauth_client_id"):
        if field in body:
            value = _string_value(body.get(field))
            merged[field] = value or None

    if "extra_headers" in body:
        extra_headers = body.get("extra_headers")
        if extra_headers in ("", None):
            merged["extra_headers"] = None
        elif not isinstance(extra_headers, dict):
            return JSONResponse(
                {"error": "extra_headers must be a JSON object"},
                status_code=400,
            )
        else:
            normalized_headers = {}
            for key, value in extra_headers.items():
                header_name = _string_value(key)
                header_value = _string_value(value)
                if not header_name or not header_value:
                    return JSONResponse(
                        {"error": "extra_headers keys and values must be non-empty strings"},
                        status_code=400,
                    )
                normalized_headers[header_name] = header_value
            merged["extra_headers"] = normalized_headers or None

    try:
        updated = ProviderConfig(**merged)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    services.config.providers[provider_name] = updated
    save_config(services.config)

    tokens = await services.store.list_tokens(provider_name)
    return JSONResponse(
        {
            "ok": True,
            "provider": _provider_summary(provider_name, updated, tokens, request),
        }
    )


@router.post("/api/providers/{provider_name}/test")
async def api_test_provider(provider_name: str, request: Request) -> JSONResponse:
    """Run a live upstream connectivity/auth test for a provider."""
    services = get_app_services(request)
    body, error = await _read_json_object(request)
    if error:
        return error

    requested_token_id = _string_value(body.get("token_id")) or None
    payload, status_code = await services.probes.test_provider(
        provider_name,
        requested_token_id=requested_token_id,
    )
    return JSONResponse(payload, status_code=status_code)


@router.delete("/api/tokens/{token_id}")
async def api_delete_token(token_id: str, request: Request) -> JSONResponse:
    """Delete a token by ID."""
    services = get_app_services(request)
    existing = await services.store.get_token(token_id)
    if not existing:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    await services.store.remove_token(token_id)
    return JSONResponse({"ok": True, "deleted": token_id})


@router.post("/api/tokens")
async def api_add_token(request: Request) -> JSONResponse:
    """Add a new token manually to the local SQLite store."""
    services = get_app_services(request)
    body, error = await _read_json_object(request)
    if error:
        return error

    token_id = _string_value(body.get("id"))
    provider = _string_value(body.get("provider"))
    access_token = _string_value(body.get("access_token"))

    if not token_id or not provider or not access_token:
        return JSONResponse(
            {"error": "id, provider, and access_token are required"},
            status_code=400,
        )

    existing = await services.store.get_token(token_id)
    if existing:
        return JSONResponse(
            {
                "error": (
                    f"Token '{token_id}' already exists. Delete it first or use a different name."
                )
            },
            status_code=409,
        )

    expires_at = None
    if body.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(body["expires_at"])
        except ValueError:
            return JSONResponse(
                {"error": "expires_at must be a valid ISO-8601 datetime"},
                status_code=400,
            )

    try:
        priority = _parse_priority(body.get("priority"), 100)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    token = Token(
        id=token_id,
        provider=provider,
        access_token=access_token,
        refresh_token=body.get("refresh_token") or None,
        token_endpoint=body.get("token_endpoint") or None,
        account_id=body.get("account_id") or None,
        oauth_client_id=body.get("oauth_client_id") or None,
        scopes=body.get("scopes") or None,
        expires_at=expires_at,
        priority=priority,
    )
    await services.store.add_token(token)
    return JSONResponse({"ok": True, "id": token_id}, status_code=201)


@router.patch("/api/tokens/{token_id}")
async def api_update_token(token_id: str, request: Request) -> JSONResponse:
    """Update mutable token metadata such as priority or status."""
    services = get_app_services(request)
    existing = await services.store.get_token(token_id)
    if not existing:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    body, error = await _read_json_object(request)
    if error:
        return error

    updates = {}
    if "priority" in body:
        try:
            updates["priority"] = _parse_priority(body["priority"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    if "status" in body:
        try:
            updates["status"] = TokenStatus(body["status"])
        except ValueError:
            return JSONResponse(
                {"error": "status must be healthy or unhealthy"},
                status_code=400,
            )

    if "access_token" in body:
        access_token = _string_value(body["access_token"])
        if access_token:
            updates["access_token"] = access_token

    if "refresh_token" in body:
        updates["refresh_token"] = _string_value(body["refresh_token"]) or None

    if "account_id" in body:
        account_id = _string_value(body["account_id"])
        if account_id:
            updates["account_id"] = account_id

    new_name = None
    if "name" in body:
        new_name = _string_value(body["name"])
        if new_name and new_name != token_id:
            if len(new_name) > 100:
                return JSONResponse({"error": "Name too long (max 100 chars)"}, status_code=400)
        else:
            new_name = None

    if not updates and new_name is None:
        return JSONResponse({"ok": True, "id": token_id, "updated": []})

    if updates:
        await services.store.update_token(token_id, **updates)

    final_id = token_id
    if new_name is not None:
        renamed = await services.store.rename_token(token_id, new_name)
        if not renamed:
            return JSONResponse({"error": f"Name '{new_name}' is already taken"}, status_code=409)
        final_id = new_name
        services.rate_limits.rename_token(token_id, new_name)

    updated_fields = sorted(updates)
    if new_name is not None:
        updated_fields.append("name")
    return JSONResponse({"ok": True, "id": final_id, "updated": updated_fields})


@router.post("/api/tokens/{token_id}/refresh")
async def api_refresh_token(token_id: str, request: Request) -> JSONResponse:
    """Manually trigger an OAuth refresh for a token."""
    services = get_app_services(request)
    token = await services.store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    if not token.refresh_token:
        return JSONResponse(
            {"error": f"Token '{token_id}' has no refresh_token"},
            status_code=400,
        )

    refreshed = await services.token_manager.refresh_token(token)
    if refreshed:
        return JSONResponse(
            {"ok": True, "id": token_id, "token": _token_to_dict(refreshed, request)}
        )

    return JSONResponse(
        {"error": f"Refresh failed for '{token_id}'. Check server logs for details."},
        status_code=502,
    )


@router.get("/api/logs")
async def api_get_logs(request: Request) -> JSONResponse:
    """Return recent proxy request logs (newest first)."""
    services = get_app_services(request)
    return JSONResponse(services.trace_store.list_logs())


@router.get("/api/logs/{log_id}")
async def api_get_log_detail(log_id: str, request: Request) -> JSONResponse:
    """Return the captured request/response trace for a proxy request."""
    services = get_app_services(request)
    detail = await services.trace_store.get_detail(log_id)
    if not detail:
        return JSONResponse({"error": f"Log entry '{log_id}' not found"}, status_code=404)
    return JSONResponse(detail)


@router.post("/api/tokens/{token_id}/enable")
async def api_enable_token(token_id: str, request: Request) -> JSONResponse:
    """Re-enable a token (mark healthy)."""
    services = get_app_services(request)
    token = await services.store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)
    await services.store.mark_healthy(token_id)
    return JSONResponse({"ok": True, "id": token_id, "status": "healthy"})


@router.post("/api/tokens/{token_id}/disable")
async def api_disable_token(token_id: str, request: Request) -> JSONResponse:
    """Disable a token (mark unhealthy so it won't be selected)."""
    services = get_app_services(request)
    token = await services.store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)
    await services.store.mark_unhealthy(token_id)
    return JSONResponse({"ok": True, "id": token_id, "status": "unhealthy"})


@router.post("/api/tokens/{token_id}/test")
async def api_test_token(token_id: str, request: Request) -> JSONResponse:
    """Send a minimal API request to validate a token works."""
    services = get_app_services(request)
    payload, status_code = await services.probes.test_token(token_id)
    return JSONResponse(payload, status_code=status_code)
