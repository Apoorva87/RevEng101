"""Core proxy logic — forward requests to upstream with auth injection."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, MutableMapping, Optional, Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from oauthrouter.models import AppConfig, ProviderConfig, Token
from oauthrouter.rate_limits import update_token_rate_limits_from_headers
from oauthrouter.token_manager import (
    NoHealthyTokensError,
    NoUsableTokensError,
    TokenManager,
)

logger = logging.getLogger(__name__)

# Headers that should NOT be forwarded to upstream (hop-by-hop or would conflict)
HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailers",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
    }
)

AUTH_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
    }
)

TRACE_REDACTED_HEADERS = frozenset(
    {
        *AUTH_HEADERS,
        "cookie",
        "set-cookie",
    }
)

# Headers from upstream response that we should NOT forward back to the caller
SKIP_RESPONSE_HEADERS = frozenset(
    {
        "transfer-encoding",
        "content-encoding",
        "content-length",
    }
)


def _body_for_trace(body: bytes) -> dict[str, Any]:
    """Represent a payload in a JSON-safe way for the debug portal."""
    if not body:
        return {
            "size_bytes": 0,
            "encoding": "utf-8",
            "text": "",
            "is_empty": True,
        }

    try:
        text = body.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        import base64

        text = base64.b64encode(body).decode("ascii")
        encoding = "base64"

    return {
        "size_bytes": len(body),
        "encoding": encoding,
        "text": text,
        "is_empty": False,
    }


def _headers_for_trace(headers: Union[dict[str, str], httpx.Headers]) -> dict[str, str]:
    """Convert headers into a stable plain dict for trace JSON."""
    traced: dict[str, str] = {}
    for key, value in headers.items():
        header_name = str(key)
        traced[header_name] = (
            "***redacted***"
            if header_name.lower() in TRACE_REDACTED_HEADERS
            else str(value)
        )
    return traced


def _record_attempt_request(
    trace: Optional[dict[str, Any]],
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    token_id: str,
) -> Optional[dict[str, Any]]:
    """Append an upstream attempt request to the trace."""
    if trace is None:
        return None

    attempt = {
        "token_id": token_id,
        "request": {
            "method": method,
            "url": url,
            "headers": _headers_for_trace(headers),
            "body": _body_for_trace(body),
        },
        "response": None,
    }
    trace.setdefault("attempts", []).append(attempt)
    return attempt


async def _record_attempt_response(
    attempt: Optional[dict[str, Any]],
    response: httpx.Response,
    *,
    body: Optional[bytes] = None,
    streaming: bool = False,
) -> Optional[bytes]:
    """Attach upstream response details to a trace attempt."""
    if body is None:
        body = await response.aread()

    if attempt is not None:
        attempt["response"] = {
            "status": response.status_code,
            "headers": _headers_for_trace(response.headers),
            "body": _body_for_trace(body),
            "streaming": streaming,
        }
    return body


def _build_upstream_url(
    provider_config: ProviderConfig, path: str, query_string: str
) -> str:
    """Construct the full upstream URL from provider base + request path."""
    url = f"{provider_config.upstream.rstrip('/')}/{path.lstrip('/')}"
    if query_string:
        url = f"{url}?{query_string}"
    return url


def _inject_auth(
    headers: dict[str, str],
    token: Token,
    provider_config: ProviderConfig,
) -> dict[str, str]:
    """Replace or add the auth header for the upstream request."""
    headers = {
        k: v
        for k, v in headers.items()
        if k.lower() not in AUTH_HEADERS
    }
    if provider_config.auth_prefix:
        headers[provider_config.auth_header] = (
            f"{provider_config.auth_prefix} {token.access_token}"
        )
    else:
        headers[provider_config.auth_header] = token.access_token

    # Inject any provider-specific extra headers (e.g. anthropic-beta for OAuth)
    if provider_config.extra_headers:
        for hdr_name, hdr_value in provider_config.extra_headers.items():
            # Merge with existing value if the header already exists (comma-separated)
            existing = headers.get(hdr_name)
            if existing:
                headers[hdr_name] = f"{existing}, {hdr_value}"
            else:
                headers[hdr_name] = hdr_value

    return headers


def _prepare_forwarded_headers(request: Request) -> dict[str, str]:
    """Extract request headers, filtering out hop-by-hop headers."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


def _parse_retry_after_value(value: str) -> Optional[datetime]:
    """Parse Retry-After as seconds or HTTP date."""
    text = value.strip()
    if not text:
        return None

    try:
        seconds = float(text)
        return datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 0))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_reset_timestamp(value: str) -> Optional[datetime]:
    """Parse an upstream reset timestamp from common formats."""
    text = value.strip()
    if not text:
        return None

    try:
        numeric = float(text)
    except ValueError:
        numeric = None

    if numeric is not None:
        if numeric > 1_000_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cooldown_until_from_headers(headers: Union[dict[str, str], httpx.Headers]) -> Optional[datetime]:
    """Derive a cooldown-until timestamp from upstream rate-limit headers."""
    retry_after = headers.get("retry-after")
    if retry_after:
        parsed = _parse_retry_after_value(str(retry_after))
        if parsed is not None:
            return parsed

    candidates: list[datetime] = []
    for header_name in (
        "anthropic-ratelimit-unified-5h-reset",
        "anthropic-ratelimit-unified-7d-reset",
    ):
        raw = headers.get(header_name)
        if raw is None:
            continue
        parsed = _parse_reset_timestamp(str(raw))
        if parsed is not None:
            candidates.append(parsed)

    future_candidates = [
        candidate for candidate in candidates
        if candidate > datetime.now(timezone.utc)
    ]
    if not future_candidates:
        return None
    return min(future_candidates)


async def forward_request(
    request: Request,
    provider: str,
    path: str,
    config: AppConfig,
    token_manager: TokenManager,
    http_client: httpx.AsyncClient,
    trace: Optional[dict[str, Any]] = None,
    rate_limit_snapshots: Optional[MutableMapping[str, dict[str, Any]]] = None,
) -> Response:
    """Forward an incoming request to the appropriate upstream provider.

    Handles:
    - Token selection (via TokenManager)
    - Auth header injection
    - Streaming and non-streaming responses
    - Auth failure detection with automatic failover
    """
    request_id = uuid.uuid4().hex[:8]
    provider_config = config.providers.get(provider)

    if not provider_config:
        logger.warning("[%s] Unknown provider: %s", request_id, provider)
        return Response(
            content=f'{{"error": "Unknown provider: {provider}"}}',
            status_code=404,
            media_type="application/json",
        )

    # Pick a token
    try:
        token = await token_manager.pick_token(provider)
    except NoHealthyTokensError as exc:
        logger.error("[%s] %s", request_id, exc)
        return Response(
            content=f'{{"error": "{exc}"}}',
            status_code=503,
            media_type="application/json",
        )
    except NoUsableTokensError as exc:
        logger.warning("[%s] %s", request_id, exc)
        payload = {"error": str(exc)}
        if exc.next_retry_at is not None:
            payload["retry_at"] = exc.next_retry_at.isoformat()
        return Response(
            content=json.dumps(payload),
            status_code=429,
            media_type="application/json",
        )

    # Build the upstream request
    upstream_url = _build_upstream_url(
        provider_config, path, request.url.query or ""
    )
    headers = _prepare_forwarded_headers(request)
    headers = _inject_auth(headers, token, provider_config)
    body = await request.body()

    if trace is not None:
        trace["incoming"] = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query": request.url.query,
            "headers": _headers_for_trace(request.headers),
            "body": _body_for_trace(body),
        }
        trace["attempts"] = []

    logger.info(
        "[%s] %s %s → %s (token=%s)",
        request_id,
        request.method,
        request.url.path,
        upstream_url,
        token.id,
    )

    def update_rate_limits(token_id: str, response: httpx.Response) -> None:
        if rate_limit_snapshots is None:
            return
        update_token_rate_limits_from_headers(
            rate_limit_snapshots,
            token_id,
            response.headers,
        )

    # Forward the request
    attempt = _record_attempt_request(
        trace,
        method=request.method,
        url=upstream_url,
        headers=headers,
        body=body,
        token_id=token.id,
    )
    response = await _do_request(
        http_client=http_client,
        method=request.method,
        url=upstream_url,
        headers=headers,
        body=body,
        request_id=request_id,
    )
    update_rate_limits(token.id, response)

    # Treat provider rate limits as token/account exhaustion and try another token.
    if response.status_code == 429:
        original_content_type = response.headers.get("content-type", "")
        original_headers = _filter_response_headers(response)
        original_body = await _record_attempt_response(attempt, response)
        await response.aclose()

        logger.warning(
            "[%s] Upstream returned 429 for token=%s, cooling down and trying failover",
            request_id,
            token.id,
        )
        cooldown_until = _cooldown_until_from_headers(response.headers)
        token_manager.mark_token_rate_limited(token.id, retry_at=cooldown_until)

        try:
            token = await token_manager.pick_token(
                provider,
                exclude_token_ids={token.id},
            )
        except (NoHealthyTokensError, NoUsableTokensError):
            logger.error(
                "[%s] No healthy tokens remain after rate limit for provider=%s",
                request_id,
                provider,
            )
            return Response(
                content=original_body,
                status_code=429,
                headers=original_headers,
                media_type=original_content_type or "application/json",
            )

        headers = _prepare_forwarded_headers(request)
        headers = _inject_auth(headers, token, provider_config)
        logger.info(
            "[%s] Retrying after 429 with token=%s → %s",
            request_id,
            token.id,
            upstream_url,
        )
        attempt = _record_attempt_request(
            trace,
            method=request.method,
            url=upstream_url,
            headers=headers,
            body=body,
            token_id=token.id,
        )
        response = await _do_request(
            http_client=http_client,
            method=request.method,
            url=upstream_url,
            headers=headers,
            body=body,
            request_id=request_id,
        )
        update_rate_limits(token.id, response)

    if response.status_code == 429:
        cooldown_until = _cooldown_until_from_headers(response.headers)
        token_manager.mark_token_rate_limited(token.id, retry_at=cooldown_until)

    # Handle auth failures with failover
    if response.status_code in (401, 403):
        logger.warning(
            "[%s] Upstream returned %d for token=%s, attempting failover",
            request_id,
            response.status_code,
            token.id,
        )
        await _record_attempt_response(attempt, response)
        await response.aclose()

        next_token = await token_manager.handle_auth_failure(token, provider)
        if next_token is None:
            return Response(
                content='{"error": "All tokens exhausted after auth failure"}',
                status_code=503,
                media_type="application/json",
            )

        # Retry with the new token
        headers = _prepare_forwarded_headers(request)
        headers = _inject_auth(headers, next_token, provider_config)
        upstream_url = _build_upstream_url(
            provider_config, path, request.url.query or ""
        )

        logger.info(
            "[%s] Retrying with token=%s → %s",
            request_id,
            next_token.id,
            upstream_url,
        )

        attempt = _record_attempt_request(
            trace,
            method=request.method,
            url=upstream_url,
            headers=headers,
            body=body,
            token_id=next_token.id,
        )
        response = await _do_request(
            http_client=http_client,
            method=request.method,
            url=upstream_url,
            headers=headers,
            body=body,
            request_id=request_id,
        )
        update_rate_limits(next_token.id, response)

        if response.status_code in (401, 403):
            logger.warning(
                "[%s] Retry also returned %d for token=%s, marking unhealthy",
                request_id,
                response.status_code,
                next_token.id,
            )
            response_body = await _record_attempt_response(attempt, response)
            await response.aclose()
            await token_manager.mark_token_unhealthy(next_token.id)
            return Response(
                content=response_body
                or b'{"error": "Token rejected after refresh; marked unhealthy"}',
                status_code=503,
                media_type="application/json",
            )

    # Check if the response is streaming (SSE)
    content_type = response.headers.get("content-type", "")
    is_streaming = "text/event-stream" in content_type

    if is_streaming:
        logger.debug("[%s] Streaming response detected, forwarding as SSE", request_id)
        return StreamingResponse(
            content=_stream_response(response, request_id, attempt),
            status_code=response.status_code,
            headers=_filter_response_headers(response),
            media_type=content_type,
        )

    # Non-streaming: read full response and return
    response_body = await _record_attempt_response(attempt, response)
    await response.aclose()

    logger.info(
        "[%s] Response: %d (%d bytes)",
        request_id,
        response.status_code,
        len(response_body),
    )

    return Response(
        content=response_body,
        status_code=response.status_code,
        headers=_filter_response_headers(response),
        media_type=content_type or "application/json",
    )


async def _do_request(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    request_id: str,
) -> httpx.Response:
    """Execute an HTTP request to the upstream, returning an un-consumed response.

    The response is returned with the body NOT yet read, so the caller can
    choose to stream it or read it in full.
    """
    logger.debug(
        "[%s] Upstream request: %s %s (body=%d bytes)",
        request_id,
        method,
        url,
        len(body),
    )

    req = http_client.build_request(
        method=method,
        url=url,
        headers=headers,
        content=body if body else None,
    )
    response = await http_client.send(req, stream=True)

    logger.debug(
        "[%s] Upstream response: %d content-type=%s",
        request_id,
        response.status_code,
        response.headers.get("content-type", "unknown"),
    )

    return response


async def _stream_response(
    response: httpx.Response,
    request_id: str,
    attempt: Optional[dict[str, Any]] = None,
) -> AsyncIterator[bytes]:
    """Yield chunks from an upstream streaming response."""
    total_bytes = 0
    chunks: list[bytes] = []
    try:
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            chunks.append(chunk)
            yield chunk
    except httpx.HTTPError as exc:
        logger.error(
            "[%s] Stream interrupted after %d bytes: %s",
            request_id,
            total_bytes,
            exc,
        )
    finally:
        if attempt is not None:
            attempt["response"] = {
                "status": response.status_code,
                "headers": _headers_for_trace(response.headers),
                "body": _body_for_trace(b"".join(chunks)),
                "streaming": True,
            }
        await response.aclose()
        logger.debug(
            "[%s] Stream completed: %d bytes total",
            request_id,
            total_bytes,
        )


def _filter_response_headers(response: httpx.Response) -> dict[str, str]:
    """Filter upstream response headers for forwarding to the caller."""
    return {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in SKIP_RESPONSE_HEADERS
    }
