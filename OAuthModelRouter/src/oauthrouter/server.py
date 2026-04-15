"""FastAPI application for OAuthModelRouter."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from oauthrouter.config import DB_PATH, load_config, save_config
from oauthrouter.models import AppConfig, ProviderConfig, Token, TokenStatus
from oauthrouter.proxy import _inject_auth, forward_request
from oauthrouter.token_manager import (
    NoHealthyTokensError,
    NoUsableTokensError,
    TokenManager,
)
from oauthrouter.token_store import TokenStore

logger = logging.getLogger(__name__)

# In-memory ring buffer of recent proxy requests (max 200 entries)
MAX_LOG_ENTRIES = 200
request_log: deque = deque(maxlen=MAX_LOG_ENTRIES)
request_details: dict[str, dict] = {}

# Per-token rate limit snapshot extracted from upstream response headers.
# Keys are token IDs; values are dicts with normalized window metadata.
token_rate_limits: dict[str, dict] = {}

PROBE_MODELS = {
    "claude": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

RATE_LIMIT_WINDOW_ALIASES = {
    "5h": ("5h", "5-hour", "5hour", "5hr"),
    "5d": ("5d", "5-day", "5day"),
    "7d": ("7d", "7-day", "7day"),
}


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


def _normalize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Lower-case response headers for case-insensitive parsing."""
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if key is None or value is None:
            continue
        normalized[str(key).lower()] = str(value)
    return normalized


def _parse_fractional_value(value: str, *, allow_overage: bool = False) -> Optional[float]:
    """Parse a utilization-like value and normalize common percentage formats."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    if numeric <= 1:
        return numeric
    # Anthropic's unified utilization headers report fractional usage and can
    # legitimately exceed 1.0 when an account is over quota, e.g. "1.01".
    if allow_overage and numeric < 10:
        return numeric
    if numeric <= 100:
        numeric /= 100.0
        return numeric
    return None


def _parse_number(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature."""
    import base64

    parts = jwt_token.split(".")
    if len(parts) != 3:
        return {}

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _resolve_openai_account_id(token: Token) -> Optional[str]:
    """Get the ChatGPT account header value for a Codex/ChatGPT OAuth token."""
    if token.account_id:
        return token.account_id

    claims = _decode_jwt_claims(token.access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    if isinstance(auth_info, dict):
        for field in ("chatgpt_user_id", "user_id", "account_id", "chatgpt_account_id"):
            value = auth_info.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def _window_label_for_seconds(limit_window_seconds: Any) -> Optional[str]:
    try:
        seconds = int(limit_window_seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _iso_from_epoch_seconds(value: Any) -> Optional[str]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1_000_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _openai_usage_snapshot(body: Any) -> Optional[dict[str, Any]]:
    """Normalize ChatGPT usage JSON into the token rate-limit snapshot shape."""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None

    allowed = bool(rate_limit.get("allowed"))
    limit_reached = bool(rate_limit.get("limit_reached"))
    snapshot: dict[str, Any] = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "windows": [],
        "overall_status": "ok" if allowed and not limit_reached else "limited",
        "allowed": allowed,
        "limit_reached": limit_reached,
    }

    plan_type = body.get("plan_type")
    if isinstance(plan_type, str) and plan_type:
        snapshot["plan_type"] = plan_type

    for _, window_data in rate_limit.items():
        if not isinstance(window_data, dict):
            continue
        label = _window_label_for_seconds(window_data.get("limit_window_seconds"))
        if not label:
            continue

        used_percent = _parse_fractional_value(str(window_data.get("used_percent", "")))
        if used_percent is None:
            used_percent = None
        reset_iso = _iso_from_epoch_seconds(window_data.get("reset_at"))
        window: dict[str, Any] = {
            "label": label,
            "status": "ok" if allowed and not limit_reached else "limited",
        }
        if used_percent is not None:
            window["utilization"] = used_percent
            snapshot[f"{label}_utilization"] = used_percent
        snapshot[f"{label}_status"] = window["status"]
        if reset_iso:
            window["reset"] = reset_iso
            snapshot[f"{label}_reset"] = reset_iso
        snapshot["windows"].append(window)

    return snapshot if snapshot["windows"] else None


def _openai_usage_ok(body: Any) -> Optional[bool]:
    """Interpret ChatGPT usage JSON as a health-check result."""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    spend_control = body.get("spend_control")
    spend_reached = (
        isinstance(spend_control, dict) and bool(spend_control.get("reached"))
    )
    return bool(rate_limit.get("allowed")) and not bool(rate_limit.get("limit_reached")) and not spend_reached


def _update_token_rate_limits_from_probe(
    token_id: str,
    provider_name: str,
    headers: Mapping[str, Any],
    body: Any,
) -> Optional[dict[str, Any]]:
    """Store the freshest rate-limit snapshot from either headers or body."""
    snapshot = _rate_limit_snapshot_from_headers(headers)
    body_snapshot = (
        _openai_usage_snapshot(body) if provider_name == "openai" else None
    )
    final_snapshot = body_snapshot or snapshot
    if final_snapshot:
        token_rate_limits[token_id] = final_snapshot
    return final_snapshot


def _rate_limit_snapshot_from_headers(headers: Mapping[str, Any]) -> Optional[dict]:
    """Parse known/provider-specific rate-limit headers into a UI snapshot."""
    normalized = _normalize_headers(headers)
    if not normalized:
        return None

    snapshot: dict[str, Any] = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "windows": [],
    }
    windows_by_label: dict[str, dict[str, Any]] = {}

    def ensure_window(label: str) -> dict[str, Any]:
        window = windows_by_label.get(label)
        if window is None:
            window = {"label": label}
            windows_by_label[label] = window
        return window

    def first_value(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            value = normalized.get(candidate)
            if value not in (None, ""):
                return value
        return None

    # Anthropic's unified headers are stable and already used elsewhere.
    for label in ("5h", "7d"):
        util_raw = normalized.get(
            f"anthropic-ratelimit-unified-{label}-utilization"
        )
        status_raw = normalized.get(f"anthropic-ratelimit-unified-{label}-status")
        reset_raw = normalized.get(f"anthropic-ratelimit-unified-{label}-reset")
        if util_raw is None and status_raw is None and reset_raw is None:
            continue

        window = ensure_window(label)
        util = (
            _parse_fractional_value(util_raw, allow_overage=True)
            if util_raw is not None
            else None
        )
        if util is not None:
            window["utilization"] = util
            snapshot[f"{label}_utilization"] = util
        elif util_raw is not None:
            logger.warning(
                "Ignoring invalid %s utilization header: %r",
                label,
                util_raw,
            )
        if status_raw is not None:
            window["status"] = status_raw
            snapshot[f"{label}_status"] = status_raw
        if reset_raw:
            window["reset"] = reset_raw
            snapshot[f"{label}_reset"] = reset_raw

    overall_status = first_value(
        "anthropic-ratelimit-unified-status",
        "x-ratelimit-status",
    )
    if overall_status:
        snapshot["overall_status"] = overall_status

    # Generic fallback for providers that expose differently-prefixed windows.
    generic_metrics: dict[str, dict[str, Any]] = {}
    for header_name, raw_value in normalized.items():
        if "ratelimit" not in header_name:
            continue

        matched_label = None
        for label, aliases in RATE_LIMIT_WINDOW_ALIASES.items():
            if any(alias in header_name for alias in aliases):
                matched_label = label
                break
        if matched_label is None:
            continue

        metric = generic_metrics.setdefault(matched_label, {})
        if "utilization" in header_name or "usage" in header_name:
            util = _parse_fractional_value(raw_value)
            if util is not None:
                metric["utilization"] = util
        elif "remaining" in header_name:
            remaining = _parse_number(raw_value)
            if remaining is not None:
                metric["remaining"] = remaining
        elif "limit" in header_name:
            limit = _parse_number(raw_value)
            if limit is not None:
                metric["limit"] = limit
        elif "status" in header_name:
            metric["status"] = raw_value
        elif "reset" in header_name:
            metric["reset"] = raw_value

    for label, metric in generic_metrics.items():
        window = ensure_window(label)
        if "utilization" not in window:
            util = metric.get("utilization")
            if util is None:
                remaining = metric.get("remaining")
                limit = metric.get("limit")
                if remaining is not None and limit not in (None, 0):
                    util = 1.0 - max(0.0, min(remaining / limit, 1.0))
            if util is not None:
                window["utilization"] = util
                snapshot.setdefault(f"{label}_utilization", util)
        if "status" not in window and metric.get("status"):
            window["status"] = metric["status"]
            snapshot.setdefault(f"{label}_status", metric["status"])
        if "reset" not in window and metric.get("reset"):
            window["reset"] = metric["reset"]
            snapshot.setdefault(f"{label}_reset", metric["reset"])

    for label in ("5h", "5d", "7d"):
        window = windows_by_label.get(label)
        if window and any(key in window for key in ("utilization", "status", "reset")):
            snapshot["windows"].append(window)

    if not snapshot["windows"] and not snapshot.get("overall_status"):
        return None
    return snapshot


def _probe_request_for_token(
    provider_name: str,
    provider_cfg: ProviderConfig,
    token: Token,
) -> tuple[str, str, dict[str, str], Optional[dict]]:
    """Build a minimal live provider probe that exercises auth + model access."""
    if provider_name == "claude":
        headers = _inject_auth({"Content-Type": "application/json"}, token, provider_cfg)
        header_names = {key.lower() for key in headers}
        if "anthropic-version" not in header_names:
            headers["anthropic-version"] = "2023-06-01"
        return (
            "POST",
            f"{provider_cfg.upstream.rstrip('/')}/v1/messages",
            headers,
            {
                "model": PROBE_MODELS["claude"],
                "max_tokens": 24,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
            },
        )

    if provider_name == "openai":
        account_id = _resolve_openai_account_id(token)
        if account_id:
            headers = _inject_auth({"Accept": "application/json"}, token, provider_cfg)
            headers["ChatGPT-Account-Id"] = account_id
            return (
                "GET",
                OPENAI_USAGE_URL,
                headers,
                None,
            )
        headers = _inject_auth({"Content-Type": "application/json"}, token, provider_cfg)
        return (
            "POST",
            f"{provider_cfg.upstream.rstrip('/')}/v1/chat/completions",
            headers,
            {
                "model": PROBE_MODELS["openai"],
                "max_tokens": 24,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
            },
        )

    raise ValueError(f"Test not implemented for provider '{provider_name}'")


def _probe_snippet(provider_name: str, body: Any, ok: bool) -> str:
    """Extract a short human-readable snippet from the probe response body."""
    if provider_name == "openai" and isinstance(body, dict) and isinstance(body.get("rate_limit"), dict):
        plan_type = body.get("plan_type") or "unknown"
        snapshot = _openai_usage_snapshot(body)
        windows = snapshot.get("windows", []) if snapshot else []
        usage_parts = []
        for window in windows:
            util = window.get("utilization")
            if util is None:
                continue
            usage_parts.append(f"{window['label']} {round(util * 100)}%")
        summary = " · ".join(usage_parts)
        return f"Plan {plan_type}" + (f" · {summary}" if summary else "")
    if ok and provider_name == "claude" and isinstance(body, dict):
        content = body.get("content", [{}])
        if content:
            return content[0].get("text", "")
        return ""
    if ok and provider_name == "openai" and isinstance(body, dict):
        choices = body.get("choices", [{}])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") in (None, "text")
            ]
            return "".join(text_parts)
        return ""
    if not ok and isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            return err.get("message", "")
    return str(body) if body is not None else ""


def _provider_summary(
    name: str,
    provider: ProviderConfig,
    tokens: list[Token],
    token_manager: Optional[TokenManager] = None,
) -> dict:
    """Serialize provider config and current token health for the UI."""
    healthy_tokens = [t for t in tokens if t.status == TokenStatus.HEALTHY]
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
    if token_manager is not None:
        for token in healthy_tokens:
            cooldown_until = token_manager.get_rate_limit_cooldown(token.id)
            if cooldown_until is not None:
                cooling_tokens.append(
                    {"id": token.id, "retry_at": cooldown_until.isoformat()}
                )

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
                "is_expired": _token_to_dict(token)["is_expired"],
                "priority": token.priority,
            }
            for token in tokens
        ],
    }


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App startup/shutdown: init DB, HTTP client, config."""
    config = load_config()
    store = TokenStore(str(DB_PATH))
    await store.init_db()
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    token_manager = TokenManager(store, http_client, config)

    app.state.config = config
    app.state.store = store
    app.state.http_client = http_client
    app.state.token_manager = token_manager

    logger.info(
        "OAuthModelRouter started on %s:%d with providers: %s",
        config.server.host,
        config.server.port,
        list(config.providers.keys()),
    )

    yield

    await http_client.aclose()
    await store.close()
    logger.info("OAuthModelRouter shut down")


app = FastAPI(
    title="OAuthModelRouter",
    description="Local reverse proxy for managing multiple OAuth tokens",
    version="0.1.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────────────────────────────
# Portal: serves the web UI at /portal
# ──────────────────────────────────────────────────────────────────────

def _static_html_response(filename: str) -> HTMLResponse:
    """Serve a static HTML file from src/oauthrouter/static."""
    from pathlib import Path

    html_path = Path(__file__).parent / "static" / filename
    return HTMLResponse(html_path.read_text())


@app.get("/portal", response_class=HTMLResponse)
async def portal_page():
    """Serve the token management web portal."""
    return _static_html_response("portal.html")


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    """Serve the built-in dashboard and endpoint walkthrough."""
    return _static_html_response("help.html")


# ──────────────────────────────────────────────────────────────────────
# API: JSON endpoints under /api/ for the portal
# ──────────────────────────────────────────────────────────────────────

def _token_to_dict(t: Token) -> dict:
    """Serialize a Token for the API, masking sensitive values."""
    now = datetime.now(timezone.utc)
    expires_at_iso = t.expires_at.isoformat() if t.expires_at else None

    is_expired = False
    expires_in_human = "unknown"
    if t.expires_at:
        # Normalize to offset-aware for comparison
        exp = t.expires_at
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

    # Mask tokens for display
    at = t.access_token
    masked = f"***{at[-8:]}" if len(at) > 12 else "***"

    d = {
        "id": t.id,
        "provider": t.provider,
        "status": t.status.value,
        "priority": t.priority,
        "is_expired": is_expired,
        "expires_at": expires_at_iso,
        "expires_in": expires_in_human,
        "has_refresh_token": t.refresh_token is not None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "masked_token": masked,
    }
    rl = token_rate_limits.get(t.id)
    if rl:
        d["rate_limits"] = rl
    return d


@app.get("/api/tokens")
async def api_list_tokens(request: Request) -> JSONResponse:
    """List all tokens with status info."""
    store: TokenStore = request.app.state.store
    tokens = await store.list_tokens()
    return JSONResponse([_token_to_dict(t) for t in tokens])


@app.get("/api/providers")
async def api_list_providers(request: Request) -> JSONResponse:
    """List provider configuration and current token coverage."""
    config: AppConfig = request.app.state.config
    store: TokenStore = request.app.state.store
    token_manager: TokenManager = request.app.state.token_manager

    providers = []
    for name, provider in config.providers.items():
        tokens = await store.list_tokens(name)
        providers.append(_provider_summary(name, provider, tokens, token_manager))

    return JSONResponse(providers)


@app.patch("/api/providers/{provider_name}")
async def api_update_provider(provider_name: str, request: Request) -> JSONResponse:
    """Update provider-level defaults and persist them to config.toml."""
    config: AppConfig = request.app.state.config
    store: TokenStore = request.app.state.store
    token_manager: TokenManager = request.app.state.token_manager
    provider = config.providers.get(provider_name)
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

    config.providers[provider_name] = updated
    save_config(config)
    request.app.state.config = config

    tokens = await store.list_tokens(provider_name)
    return JSONResponse(
        {
            "ok": True,
            "provider": _provider_summary(
                provider_name,
                updated,
                tokens,
                token_manager,
            ),
        }
    )


@app.post("/api/providers/{provider_name}/test")
async def api_test_provider(provider_name: str, request: Request) -> JSONResponse:
    """Run a live upstream connectivity/auth test for a provider."""
    config: AppConfig = request.app.state.config
    store: TokenStore = request.app.state.store
    token_manager: TokenManager = request.app.state.token_manager
    http_client: httpx.AsyncClient = request.app.state.http_client
    provider = config.providers.get(provider_name)
    if not provider:
        return JSONResponse({"error": f"Provider '{provider_name}' not found"}, status_code=404)

    body, error = await _read_json_object(request)
    if error:
        return error
    requested_token_id = _string_value(body.get("token_id"))

    async def run_probe(token: Token) -> tuple[Optional[httpx.Response], Optional[JSONResponse]]:
        try:
            method, probe_url, headers, probe_body = _probe_request_for_token(
                provider_name,
                provider,
                token,
            )
        except ValueError as exc:
            return None, JSONResponse({"error": str(exc)}, status_code=400)
        started = time.monotonic()
        try:
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": httpx.Timeout(20.0, connect=10.0),
            }
            if probe_body is not None:
                request_kwargs["json"] = probe_body
            response = await http_client.request(method, probe_url, **request_kwargs)
            return response, None
        except httpx.HTTPError as exc:
            return None, JSONResponse(
                {
                    "ok": False,
                    "provider": provider_name,
                    "upstream": provider.upstream,
                    "test_url": probe_url,
                    "token_id": token.id,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "error": str(exc),
                },
                status_code=502,
            )

    if requested_token_id:
        token = await store.get_token(requested_token_id)
        if token is None or token.provider != provider_name:
            return JSONResponse(
                {"error": f"Token '{requested_token_id}' is not available for provider '{provider_name}'"},
                status_code=404,
            )
    else:
        try:
            token = await token_manager.pick_token(provider_name)
        except NoHealthyTokensError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except NoUsableTokensError as exc:
            payload = {"error": str(exc)}
            if exc.next_retry_at is not None:
                payload["retry_at"] = exc.next_retry_at.isoformat()
            return JSONResponse(payload, status_code=429)

    started = time.monotonic()
    attempts = 1
    attempt_details: list[dict[str, Any]] = []
    response, error_response = await run_probe(token)
    if error_response is not None:
        return error_response
    assert response is not None
    attempt_details.append(
        {
            "token_id": token.id,
            "response": {"headers": dict(response.headers)},
        }
    )
    _, test_url, _, _ = _probe_request_for_token(provider_name, provider, token)

    notes: list[str] = []

    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        try:
            retry_seconds = float(retry_after) if retry_after else None
        except (TypeError, ValueError):
            retry_seconds = None
        token_manager.mark_token_rate_limited(
            token.id,
            retry_after_seconds=retry_seconds,
        )
        notes.append(f"{token.id} entered cooldown after 429")

        if requested_token_id:
            next_token = None
        else:
            try:
                next_token = await token_manager.pick_token(
                    provider_name,
                    exclude_token_ids={token.id},
                )
            except (NoHealthyTokensError, NoUsableTokensError):
                next_token = None

        if next_token is not None:
            attempts += 1
            token = next_token
            response, error_response = await run_probe(token)
            if error_response is not None:
                return error_response
            assert response is not None
            attempt_details.append(
                {
                    "token_id": token.id,
                    "response": {"headers": dict(response.headers)},
                }
            )
            _, test_url, _, _ = _probe_request_for_token(provider_name, provider, token)
            notes.append(f"Retried with {token.id}")

    if response.status_code in (401, 403):
        next_token = await token_manager.handle_auth_failure(token, provider_name)
        if requested_token_id:
            next_token = None
        if next_token is not None:
            attempts += 1
            token = next_token
            response, error_response = await run_probe(token)
            if error_response is not None:
                return error_response
            assert response is not None
            attempt_details.append(
                {
                    "token_id": token.id,
                    "response": {"headers": dict(response.headers)},
                }
            )
            _, test_url, _, _ = _probe_request_for_token(provider_name, provider, token)
            notes.append(f"Auth failover retried with {token.id}")

    retry_after = response.headers.get("retry-after")
    if response.status_code == 429:
        try:
            retry_seconds = float(retry_after) if retry_after else None
        except (TypeError, ValueError):
            retry_seconds = None
        token_manager.mark_token_rate_limited(token.id, retry_after_seconds=retry_seconds)

    try:
        response_body = response.json()
    except Exception:
        response_body = response.text[:500]

    ok = response.is_success
    openai_ok = _openai_usage_ok(response_body) if provider_name == "openai" else None
    if openai_ok is not None:
        ok = ok and openai_ok
    snippet = _probe_snippet(provider_name, response_body, ok)
    _extract_rate_limits(attempt_details)
    rate_limits = _update_token_rate_limits_from_probe(
        token.id,
        provider_name,
        dict(response.headers),
        response_body,
    )

    latency_ms = round((time.monotonic() - started) * 1000)
    payload = {
        "ok": ok,
        "provider": provider_name,
        "upstream": provider.upstream,
        "test_url": test_url,
        "token_id": token.id,
        "latency_ms": latency_ms,
        "status_code": response.status_code,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "retry_after": retry_after,
        "attempts": attempts,
        "notes": notes,
        "snippet": snippet,
    }
    if rate_limits:
        payload["rate_limits"] = rate_limits

    if ok and token.status == TokenStatus.UNHEALTHY:
        await store.mark_healthy(token.id)

    if ok:
        return JSONResponse(payload)

    return JSONResponse(
        {
            **payload,
            "error": snippet or response.text[:300] or "Provider test failed",
        },
        status_code=response.status_code,
    )


@app.delete("/api/tokens/{token_id}")
async def api_delete_token(token_id: str, request: Request) -> JSONResponse:
    """Delete a token by ID."""
    store: TokenStore = request.app.state.store
    existing = await store.get_token(token_id)
    if not existing:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    await store.remove_token(token_id)
    logger.info("Portal: deleted token %s", token_id)
    return JSONResponse({"ok": True, "deleted": token_id})


@app.post("/api/tokens")
async def api_add_token(request: Request) -> JSONResponse:
    """Add a new token manually.

    Body: { "id": "...", "provider": "...", "access_token": "...",
            "refresh_token": "...", "expires_at": "..." }
    """
    store: TokenStore = request.app.state.store
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

    # Check for duplicates
    existing = await store.get_token(token_id)
    if existing:
        return JSONResponse(
            {"error": f"Token '{token_id}' already exists. Delete it first or use a different name."},
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
    await store.add_token(token)
    logger.info("Portal: added token %s for provider %s", token_id, provider)
    return JSONResponse({"ok": True, "id": token_id}, status_code=201)


@app.patch("/api/tokens/{token_id}")
async def api_update_token(token_id: str, request: Request) -> JSONResponse:
    """Update mutable token metadata such as priority or status."""
    store: TokenStore = request.app.state.store
    existing = await store.get_token(token_id)
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
            return JSONResponse({"error": "status must be healthy or unhealthy"}, status_code=400)

    if "access_token" in body:
        at = _string_value(body["access_token"])
        if at:
            updates["access_token"] = at

    if "refresh_token" in body:
        rt = _string_value(body["refresh_token"]) or None
        updates["refresh_token"] = rt

    if "account_id" in body:
        account_id = _string_value(body["account_id"])
        if account_id:
            updates["account_id"] = account_id

    # Handle rename separately — it changes the primary key
    new_name = None
    if "name" in body:
        new_name = _string_value(body["name"])
        if new_name and new_name != token_id:
            # Validate: no special chars that would break things
            if len(new_name) > 100:
                return JSONResponse({"error": "Name too long (max 100 chars)"}, status_code=400)
        else:
            new_name = None  # no-op if same name or empty

    if not updates and new_name is None:
        return JSONResponse({"ok": True, "id": token_id, "updated": []})

    # Apply field updates first
    if updates:
        await store.update_token(token_id, **updates)

    # Then rename if requested
    final_id = token_id
    if new_name is not None:
        renamed = await store.rename_token(token_id, new_name)
        if not renamed:
            return JSONResponse(
                {"error": f"Name '{new_name}' is already taken"},
                status_code=409,
            )
        final_id = new_name
        # Migrate rate limit snapshot to new name
        if token_id in token_rate_limits:
            token_rate_limits[new_name] = token_rate_limits.pop(token_id)

    updated_fields = sorted(updates)
    if new_name is not None:
        updated_fields.append("name")
    logger.info("Portal: updated token %s fields=%s", token_id, updated_fields)
    return JSONResponse({"ok": True, "id": final_id, "updated": updated_fields})


@app.post("/api/tokens/{token_id}/refresh")
async def api_refresh_token(token_id: str, request: Request) -> JSONResponse:
    """Manually trigger an OAuth refresh for a token.

    The token must have a refresh_token and a resolvable token_endpoint.
    """
    store: TokenStore = request.app.state.store
    token_manager: TokenManager = request.app.state.token_manager

    token = await store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    if not token.refresh_token:
        return JSONResponse(
            {"error": f"Token '{token_id}' has no refresh_token"},
            status_code=400,
        )

    logger.info("Portal: manually refreshing token %s", token_id)
    refreshed = await token_manager.refresh_token(token)

    if refreshed:
        logger.info("Portal: token %s refreshed successfully", token_id)
        return JSONResponse({
            "ok": True,
            "id": token_id,
            "token": _token_to_dict(refreshed),
        })
    else:
        logger.warning("Portal: token %s refresh failed", token_id)
        return JSONResponse(
            {"error": f"Refresh failed for '{token_id}'. Check server logs for details."},
            status_code=502,
        )


# ──────────────────────────────────────────────────────────────────────
# Health + catch-all proxy (must be LAST)
# ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Health check endpoint showing token status per provider."""
    store: TokenStore = request.app.state.store
    config: AppConfig = request.app.state.config

    providers_health = {}
    for provider_name in config.providers:
        all_tokens = await store.list_tokens(provider_name)
        healthy = [t for t in all_tokens if t.status.value == "healthy"]
        providers_health[provider_name] = {
            "healthy_tokens": len(healthy),
            "total_tokens": len(all_tokens),
            "token_names": [
                {"name": t.id, "status": t.status.value} for t in all_tokens
            ],
        }

    overall = all(
        info["healthy_tokens"] > 0 for info in providers_health.values()
    ) if providers_health else False

    return JSONResponse({
        "status": "ok" if overall else "degraded",
        "providers": providers_health,
    })


@app.get("/api/logs")
async def api_get_logs(request: Request) -> JSONResponse:
    """Return recent proxy request logs (newest first)."""
    return JSONResponse(list(reversed(request_log)))


@app.get("/api/logs/{log_id}")
async def api_get_log_detail(log_id: str, request: Request) -> JSONResponse:
    """Return the captured request/response trace for a proxy request."""
    detail = request_details.get(log_id)
    if not detail:
        return JSONResponse({"error": f"Log entry '{log_id}' not found"}, status_code=404)
    return JSONResponse(detail)


@app.post("/api/tokens/{token_id}/enable")
async def api_enable_token(token_id: str, request: Request) -> JSONResponse:
    """Re-enable a token (mark healthy)."""
    store: TokenStore = request.app.state.store
    token = await store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)
    await store.mark_healthy(token_id)
    logger.info("Portal: token %s enabled (marked healthy)", token_id)
    return JSONResponse({"ok": True, "id": token_id, "status": "healthy"})


@app.post("/api/tokens/{token_id}/disable")
async def api_disable_token(token_id: str, request: Request) -> JSONResponse:
    """Disable a token (mark unhealthy so it won't be selected)."""
    store: TokenStore = request.app.state.store
    token = await store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)
    await store.mark_unhealthy(token_id)
    logger.info("Portal: token %s disabled (marked unhealthy)", token_id)
    return JSONResponse({"ok": True, "id": token_id, "status": "unhealthy"})


@app.post("/api/tokens/{token_id}/test")
async def api_test_token(token_id: str, request: Request) -> JSONResponse:
    """Send a minimal API request to validate a token works."""
    store: TokenStore = request.app.state.store
    token = await store.get_token(token_id)
    if not token:
        return JSONResponse({"error": f"Token '{token_id}' not found"}, status_code=404)

    config: AppConfig = request.app.state.config
    provider_cfg = config.providers.get(token.provider)
    if not provider_cfg:
        return JSONResponse(
            {"error": f"No provider config for '{token.provider}'"}, status_code=400
        )

    http_client: httpx.AsyncClient = request.app.state.http_client

    try:
        method, url, headers, body = _probe_request_for_token(
            token.provider,
            provider_cfg,
            token,
        )
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=400,
        )

    try:
        started = time.monotonic()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": 15.0}
        if body is not None:
            request_kwargs["json"] = body
        resp = await http_client.request(method, url, **request_kwargs)
        status = resp.status_code
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text[:500]

        ok = 200 <= status < 300
        openai_ok = _openai_usage_ok(resp_body) if token.provider == "openai" else None
        if openai_ok is not None:
            ok = ok and openai_ok
        snippet = _probe_snippet(token.provider, resp_body, ok)

        # If test succeeds and token was unhealthy, mark healthy
        if ok and token.status == TokenStatus.UNHEALTHY:
            await store.mark_healthy(token_id)

        # Extract rate limits from the response headers
        _extract_rate_limits(
            [{"token_id": token_id, "response": {"headers": dict(resp.headers)}}]
        )
        rate_limits = _update_token_rate_limits_from_probe(
            token_id,
            token.provider,
            dict(resp.headers),
            resp_body,
        )

        payload = {
            "ok": ok,
            "status": status,
            "snippet": snippet,
            "token_id": token_id,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
        if rate_limits:
            payload["rate_limits"] = rate_limits
        return JSONResponse(payload)
    except httpx.TimeoutException:
        return JSONResponse(
            {
                "ok": False,
                "status": 0,
                "snippet": "Request timed out",
                "token_id": token_id,
            }
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "status": 0,
                "snippet": str(exc),
                "token_id": token_id,
            }
        )


@app.api_route(
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


@app.api_route(
    "/{provider}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_route(provider: str, path: str, request: Request) -> Response:
    """Catch-all proxy route — must be registered last."""
    config: AppConfig = request.app.state.config
    token_manager: TokenManager = request.app.state.token_manager
    http_client: httpx.AsyncClient = request.app.state.http_client

    req_id = uuid.uuid4().hex[:8]
    start = time.monotonic()
    trace = {
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "warning": "This trace is stored in memory only and may contain live authorization headers.",
    }

    response = await forward_request(
        request=request,
        provider=provider,
        path=path,
        config=config,
        token_manager=token_manager,
        http_client=http_client,
        trace=trace,
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
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "path": f"/{provider}/{path}",
        "provider": provider,
        "token_used": used_token,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "client": request.client.host if request.client else "unknown",
        "has_detail": True,
        "attempts": len(trace.get("attempts", [])),
    }
    request_log.append(log_entry)
    request_details[req_id] = trace

    # Extract rate limit info from the last attempt's response headers.
    _extract_rate_limits(attempts)

    valid_ids = {entry["id"] for entry in request_log}
    for detail_id in list(request_details):
        if detail_id not in valid_ids:
            request_details.pop(detail_id, None)

    return response


def _extract_rate_limits(attempts: list[dict]) -> None:
    """Pull provider rate-limit headers from each attempt into the token snapshot."""
    for attempt in attempts:
        token_id = attempt.get("token_id")
        resp = attempt.get("response") or {}
        headers = resp.get("headers") or {}
        if not token_id or not headers:
            continue

        snapshot = _rate_limit_snapshot_from_headers(headers)
        if snapshot:
            token_rate_limits[token_id] = snapshot
