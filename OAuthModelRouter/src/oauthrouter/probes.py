"""Shared probe helpers for provider and token test endpoints."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from oauthrouter.models import AppConfig, ProviderConfig, Token, TokenStatus
from oauthrouter.proxy import _inject_auth
from oauthrouter.rate_limits import openai_usage_ok, openai_usage_snapshot
from oauthrouter.rate_limit_store import RateLimitStore
from oauthrouter.token_manager import (
    NoHealthyTokensError,
    NoUsableTokensError,
    TokenManager,
)
from oauthrouter.token_store import TokenStore

PROBE_MODELS = {
    "claude": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
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


def resolve_openai_account_id(token: Token) -> Optional[str]:
    """Get the ChatGPT account header value for a Codex/ChatGPT OAuth token."""
    if token.account_id:
        return token.account_id

    claims = decode_jwt_claims(token.access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    if isinstance(auth_info, dict):
        for field in ("chatgpt_user_id", "user_id", "account_id", "chatgpt_account_id"):
            value = auth_info.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def probe_request_for_token(
    provider_name: str,
    provider_cfg: ProviderConfig,
    token: Token,
) -> tuple[str, str, dict[str, str], Optional[dict[str, Any]]]:
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
        account_id = resolve_openai_account_id(token)
        if account_id:
            headers = _inject_auth({"Accept": "application/json"}, token, provider_cfg)
            headers["ChatGPT-Account-Id"] = account_id
            return ("GET", OPENAI_USAGE_URL, headers, None)
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


def probe_snippet(provider_name: str, body: Any, ok: bool) -> str:
    """Extract a short human-readable snippet from the probe response body."""
    if (
        provider_name == "openai"
        and isinstance(body, dict)
        and isinstance(body.get("rate_limit"), dict)
    ):
        plan_type = body.get("plan_type") or "unknown"
        snapshot = openai_usage_snapshot(body)
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


class ProbeService:
    """Executes provider and token probes with shared parsing and bookkeeping."""

    def __init__(
        self,
        config: AppConfig,
        store: TokenStore,
        token_manager: TokenManager,
        http_client: httpx.AsyncClient,
        rate_limits: RateLimitStore,
    ) -> None:
        self._config = config
        self._store = store
        self._token_manager = token_manager
        self._http_client = http_client
        self._rate_limits = rate_limits

    async def test_provider(
        self,
        provider_name: str,
        *,
        requested_token_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], int]:
        provider = self._config.providers.get(provider_name)
        if not provider:
            return {"error": f"Provider '{provider_name}' not found"}, 404

        if requested_token_id:
            token = await self._store.get_token(requested_token_id)
            if token is None or token.provider != provider_name:
                return {
                    "error": (
                        f"Token '{requested_token_id}' is not available for "
                        f"provider '{provider_name}'"
                    )
                }, 404
        else:
            selection_error = await self._pick_provider_token(provider_name)
            if isinstance(selection_error, tuple):
                return selection_error
            token = selection_error

        started = time.monotonic()
        notes: list[str] = []
        attempts = 0
        can_failover = not requested_token_id

        # Try up to 2 attempts: initial probe + one retry on 429 or auth failure
        for _ in range(2):
            call = await self._request_probe(
                provider_name,
                provider,
                token,
                timeout_seconds=20.0,
            )
            if call["error"] is not None:
                return call["error"], call["status_code"]

            response = call["response"]
            test_url = call["test_url"]
            assert response is not None
            assert test_url is not None

            attempts += 1
            self._rate_limits.update_from_headers(token.id, dict(response.headers))

            # Rate-limited: cool down this token and try the next one
            if response.status_code == 429 and can_failover and attempts == 1:
                self._mark_token_rate_limited(token.id, response.headers)
                notes.append(f"{token.id} entered cooldown after 429")
                next_token = await self._pick_next_provider_token(provider_name, token.id)
                if next_token is not None:
                    token = next_token
                    notes.append(f"Retried with {token.id}")
                    continue

            # Auth failure: try refresh + failover
            if response.status_code in (401, 403) and attempts == 1:
                recovered = await self._token_manager.handle_auth_failure(token, provider_name)
                if can_failover and recovered is not None:
                    token = recovered
                    notes.append(f"Auth failover retried with {token.id}")
                    continue

            # Also mark rate-limited on 429 even when we can't failover
            if response.status_code == 429:
                self._mark_token_rate_limited(token.id, response.headers)
                notes.append(f"{token.id} entered cooldown after 429")

            break

        return await self._build_probe_result(
            provider_name=provider_name,
            token=token,
            response=response,
            started=started,
            provider=provider,
            test_url=test_url,
            attempts=attempts,
            notes=notes,
        )

    async def test_token(self, token_id: str) -> tuple[dict[str, Any], int]:
        token = await self._store.get_token(token_id)
        if not token:
            return {"error": f"Token '{token_id}' not found"}, 404

        provider = self._config.providers.get(token.provider)
        if not provider:
            return {"error": f"No provider config for '{token.provider}'"}, 400

        started = time.monotonic()
        call = await self._request_probe(
            token.provider,
            provider,
            token,
            timeout_seconds=15.0,
        )
        if call["error"] is not None:
            error = call["error"]
            if call["status_code"] >= 500 and error.get("status") == 0:
                return error, 200
            return error, call["status_code"]

        response = call["response"]
        assert response is not None
        self._rate_limits.update_from_headers(token_id, dict(response.headers))

        return await self._build_probe_result(
            provider_name=token.provider,
            token=token,
            response=response,
            started=started,
        )

    async def _pick_provider_token(
        self,
        provider_name: str,
    ) -> Token | tuple[dict[str, Any], int]:
        try:
            return await self._token_manager.pick_token(provider_name)
        except NoHealthyTokensError as exc:
            return {"error": str(exc)}, 503
        except NoUsableTokensError as exc:
            payload = {"error": str(exc)}
            if exc.next_retry_at is not None:
                payload["retry_at"] = exc.next_retry_at.isoformat()
            return payload, 429

    async def _pick_next_provider_token(
        self,
        provider_name: str,
        excluded_token_id: str,
    ) -> Optional[Token]:
        try:
            return await self._token_manager.pick_token(
                provider_name,
                exclude_token_ids={excluded_token_id},
            )
        except (NoHealthyTokensError, NoUsableTokensError):
            return None

    async def _request_probe(
        self,
        provider_name: str,
        provider: ProviderConfig,
        token: Token,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            method, probe_url, headers, probe_body = probe_request_for_token(
                provider_name,
                provider,
                token,
            )
        except ValueError as exc:
            return {
                "response": None,
                "test_url": None,
                "error": {"error": str(exc)},
                "status_code": 400,
            }

        started = time.monotonic()
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": httpx.Timeout(timeout_seconds, connect=10.0),
        }
        if probe_body is not None:
            request_kwargs["json"] = probe_body

        try:
            response = await self._http_client.request(method, probe_url, **request_kwargs)
        except httpx.TimeoutException:
            return {
                "response": None,
                "test_url": probe_url,
                "error": {
                    "ok": False,
                    "provider": provider_name,
                    "upstream": provider.upstream,
                    "test_url": probe_url,
                    "token_id": token.id,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "error": "Request timed out",
                    "status": 0,
                    "snippet": "Request timed out",
                },
                "status_code": 502,
            }
        except httpx.HTTPError as exc:
            return {
                "response": None,
                "test_url": probe_url,
                "error": {
                    "ok": False,
                    "provider": provider_name,
                    "upstream": provider.upstream,
                    "test_url": probe_url,
                    "token_id": token.id,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "error": str(exc),
                    "status": 0,
                    "snippet": str(exc),
                },
                "status_code": 502,
            }

        return {
            "response": response,
            "test_url": probe_url,
            "error": None,
            "status_code": response.status_code,
        }

    async def _build_probe_result(
        self,
        *,
        provider_name: str,
        token: Token,
        response: httpx.Response,
        started: float,
        # Provider-test-only fields (omitted for token tests)
        provider: Optional[ProviderConfig] = None,
        test_url: Optional[str] = None,
        attempts: Optional[int] = None,
        notes: Optional[list[str]] = None,
    ) -> tuple[dict[str, Any], int]:
        response_body = self._response_body(response)
        ok = self._response_ok(provider_name, response, response_body)
        snippet = probe_snippet(provider_name, response_body, ok)
        rate_limits = self._rate_limits.update_from_probe(
            token.id,
            provider_name,
            dict(response.headers),
            response_body,
        )

        if ok and token.status == TokenStatus.UNHEALTHY:
            await self._store.mark_healthy(token.id)

        payload: dict[str, Any] = {
            "ok": ok,
            "token_id": token.id,
            "snippet": snippet,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

        # Provider-level probes include richer routing context
        if provider is not None:
            payload.update({
                "provider": provider_name,
                "upstream": provider.upstream,
                "test_url": test_url,
                "status_code": response.status_code,
                "retry_after": response.headers.get("retry-after"),
                "attempts": attempts,
                "notes": notes,
            })
        else:
            payload["status"] = response.status_code

        if rate_limits:
            payload["rate_limits"] = rate_limits

        if ok:
            return payload, 200

        payload["error"] = snippet or response.text[:300] or "Provider test failed"
        return payload, response.status_code

    @staticmethod
    def _response_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text[:500]

    @staticmethod
    def _response_ok(provider_name: str, response: httpx.Response, body: Any) -> bool:
        ok = response.is_success
        provider_ok = openai_usage_ok(body) if provider_name == "openai" else None
        if provider_ok is not None:
            ok = ok and provider_ok
        return ok

    def _mark_token_rate_limited(
        self,
        token_id: str,
        headers: httpx.Headers,
    ) -> None:
        retry_after = headers.get("retry-after")
        try:
            retry_seconds = float(retry_after) if retry_after else None
        except (TypeError, ValueError):
            retry_seconds = None
        self._token_manager.mark_token_rate_limited(
            token_id,
            retry_after_seconds=retry_seconds,
        )
