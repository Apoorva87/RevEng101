"""Tests for token discovery helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from oauthrouter.discover import discover_claude_tokens, discovered_token_to_token


def test_discover_claude_tokens_preserves_refresh_metadata(monkeypatch):
    """Keychain discovery keeps oauth_client_id and scopes for refresh."""
    future_exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
    raw = json.dumps(
        {
            "oauthClientId": "client-123",
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abcdef",
                "refreshToken": "sk-ant-ort01-abcdef",
                "expiresAt": future_exp,
                "scopes": ["user:inference", "org:create_api_key"],
                "subscriptionType": "pro",
            },
        }
    )

    monkeypatch.setattr(
        "oauthrouter.discover.CLAUDE_KEYCHAIN_SERVICES",
        ["Claude Code-credentials"],
    )
    monkeypatch.setattr(
        "oauthrouter.discover._read_keychain_password",
        lambda service, account=None: raw if service == "Claude Code-credentials" else None,
    )

    discovered = discover_claude_tokens()

    assert len(discovered) == 1
    assert discovered[0].oauth_client_id == "client-123"
    assert discovered[0].scopes == ["user:inference", "org:create_api_key"]

    stored = discovered_token_to_token(discovered[0], token_id="claude-pro", priority=5)
    assert stored.oauth_client_id == "client-123"
    assert stored.scopes == "user:inference org:create_api_key"
    assert stored.priority == 5
