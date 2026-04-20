"""Shared helpers for handling OpenAI / ChatGPT OAuth tokens."""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from oauthrouter.models import Token


def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature."""
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
