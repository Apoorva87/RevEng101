"""Data models for OAuthModelRouter."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class TokenStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class Token(BaseModel):
    """A stored OAuth token for a provider."""

    id: str = Field(description="User-friendly name like 'claude-personal'")
    provider: str = Field(description="Provider key: 'claude' or 'openai'")
    access_token: str
    refresh_token: Optional[str] = None
    token_endpoint: Optional[str] = None
    account_id: Optional[str] = Field(
        default=None,
        description="Provider-specific account identifier (e.g. Codex auth.json account_id)",
    )
    oauth_client_id: Optional[str] = Field(
        default=None,
        description="Per-token OAuth client_id (e.g. UUID from keychain oauthClientId)",
    )
    scopes: Optional[str] = Field(
        default=None,
        description="Space-separated OAuth scopes for refresh requests",
    )
    expires_at: Optional[datetime] = None
    status: TokenStatus = TokenStatus.HEALTHY
    priority: int = Field(
        default=100,
        description="Lower values are selected first within a provider",
    )
    last_used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def is_token_expired(token: Token) -> bool:
    """Check if a token's access_token has expired (timezone-safe)."""
    if token.expires_at is None:
        return False
    exp = token.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


class ProviderConfig(BaseModel):
    """Configuration for a single upstream provider."""

    upstream: str = Field(description="Base URL of the upstream API")
    auth_header: str = Field(
        default="Authorization",
        description="HTTP header name for the auth token",
    )
    auth_prefix: Optional[str] = Field(
        default=None,
        description="Prefix before the token value (e.g. 'Bearer')",
    )
    token_endpoint: Optional[str] = Field(
        default=None,
        description="OAuth token refresh endpoint URL",
    )
    oauth_client_id: Optional[str] = Field(
        default=None,
        description="OAuth client_id for token refresh requests",
    )
    extra_headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Extra headers to inject into every upstream request for this provider",
    )


class ServerConfig(BaseModel):
    """Server bind configuration."""

    host: str = "127.0.0.1"
    port: int = 8000


class AppConfig(BaseModel):
    """Top-level application configuration."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
