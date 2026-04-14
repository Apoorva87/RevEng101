"""Discover existing OAuth tokens on this machine.

Sources:
  - Codex (OpenAI): ~/.codex/auth.json — plain JSON with OAuth tokens
  - Claude Code:    macOS Keychain, service "Claude Code-credentials" — JSON blob
                    with claudeAiOauth containing accessToken/refreshToken
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from oauthrouter.models import Token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes for discovered tokens
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredToken:
    """A token found on the local machine."""

    source: str             # e.g. "codex:~/.codex/auth.json", "keychain:Claude Code-credentials"
    provider: str           # "openai" or "claude"
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    account_id: Optional[str] = None
    subscription_type: Optional[str] = None
    oauth_client_id: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def expires_in_human(self) -> str:
        if self.expires_at is None:
            return "unknown"
        delta = self.expires_at - datetime.now(timezone.utc)
        if delta.total_seconds() < 0:
            return f"EXPIRED {abs(int(delta.total_seconds()))}s ago"
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def masked_token(self, token_value: str, show_chars: int = 8) -> str:
        """Show only the last N chars of a token for safe display."""
        if len(token_value) <= show_chars:
            return "***"
        return f"***{token_value[-show_chars:]}"


# ---------------------------------------------------------------------------
# Codex (OpenAI) discovery
# ---------------------------------------------------------------------------

def _decode_jwt_exp(jwt_token: str) -> Optional[datetime]:
    """Extract expiration time from a JWT without verifying the signature."""
    import base64

    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            logger.debug("JWT does not have 3 parts, cannot decode exp")
            return None

        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
        return None
    except Exception as exc:
        logger.debug("Failed to decode JWT: %s", exc)
        return None


def discover_codex_tokens() -> List[DiscoveredToken]:
    """Discover OpenAI OAuth tokens from ~/.codex/auth.json.

    The Codex CLI stores tokens as:
    {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "<JWT>",
            "refresh_token": "<string>",
            "account_id": "<uuid>"
        },
        "last_refresh": "<ISO datetime>"
    }
    """
    auth_path = Path.home() / ".codex" / "auth.json"
    source = f"codex:{auth_path}"

    if not auth_path.exists():
        logger.info("Codex auth file not found at %s", auth_path)
        return []

    logger.info("Found Codex auth file at %s", auth_path)

    try:
        data = json.loads(auth_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read Codex auth file: %s", exc)
        return [DiscoveredToken(
            source=source,
            provider="openai",
            access_token="",
            error=f"Failed to read: {exc}",
        )]

    auth_mode = data.get("auth_mode")
    logger.debug("Codex auth_mode: %s", auth_mode)

    tokens_data = data.get("tokens")
    if not tokens_data:
        # Check for direct API key
        api_key = data.get("OPENAI_API_KEY")
        if api_key:
            logger.info("Codex has a direct API key (not OAuth)")
            return [DiscoveredToken(
                source=source,
                provider="openai",
                access_token=api_key,
                subscription_type="api_key",
            )]
        logger.warning("Codex auth file has no tokens and no API key")
        return []

    access_token = tokens_data.get("access_token", "")
    refresh_token = tokens_data.get("refresh_token")
    account_id = tokens_data.get("account_id")

    # Decode JWT to get expiration
    expires_at = _decode_jwt_exp(access_token) if access_token else None

    # Try to extract plan type from the JWT claims
    subscription_type = None
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) == 3:
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            auth_info = claims.get("https://api.openai.com/auth", {})
            subscription_type = auth_info.get("chatgpt_plan_type")
    except Exception:
        pass

    token = DiscoveredToken(
        source=source,
        provider="openai",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        account_id=account_id,
        subscription_type=subscription_type or auth_mode,
    )

    logger.info(
        "Discovered Codex token: account=%s, plan=%s, expires=%s, has_refresh=%s",
        account_id,
        subscription_type,
        token.expires_in_human,
        refresh_token is not None,
    )

    return [token]


# ---------------------------------------------------------------------------
# Claude Code discovery (macOS Keychain)
# ---------------------------------------------------------------------------

CLAUDE_KEYCHAIN_SERVICES = [
    "Claude Code-credentials",
    "Claude Code-credentials-2",
]


def _read_keychain_password(service: str, account: Optional[str] = None) -> Optional[str]:
    """Read a generic password from the macOS login keychain.

    Uses `security find-generic-password -s <service> -w` which may trigger
    a macOS authorization dialog.
    """
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd.extend(["-a", account])

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "could not be found" in stderr or "SecKeychainSearchCopyNext" in stderr:
                logger.debug("Keychain item not found: service=%s", service)
                return None
            logger.warning(
                "Keychain read failed for service=%s: returncode=%d stderr=%s",
                service,
                result.returncode,
                stderr,
            )
            return None

        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error(
            "Keychain read timed out for service=%s (user may need to authorize)",
            service,
        )
        return None
    except FileNotFoundError:
        logger.error("'security' command not found — not running on macOS?")
        return None


def discover_claude_tokens() -> List[DiscoveredToken]:
    """Discover Claude OAuth tokens from the macOS Keychain.

    Claude Code stores credentials in the keychain under service names like
    "Claude Code-credentials". The value is a JSON blob:
    {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-...",
            "refreshToken": "sk-ant-ort01-...",
            "expiresAt": 1776019431085,   // epoch milliseconds
            "scopes": ["user:inference", ...],
            "subscriptionType": "pro",
            "rateLimitTier": "default_claude_ai"
        }
    }
    """
    discovered: List[DiscoveredToken] = []

    for service in CLAUDE_KEYCHAIN_SERVICES:
        source = f"keychain:{service}"
        logger.info("Checking keychain for service: %s", service)

        raw = _read_keychain_password(service)
        if raw is None:
            logger.debug("No credential found for service: %s", service)
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse keychain JSON for %s: %s", service, exc)
            discovered.append(DiscoveredToken(
                source=source,
                provider="claude",
                access_token="",
                error=f"Invalid JSON in keychain: {exc}",
            ))
            continue

        oauth = data.get("claudeAiOauth")
        if not oauth:
            logger.debug("No claudeAiOauth in keychain data for %s", service)
            continue

        access_token = oauth.get("accessToken", "")
        refresh_token = oauth.get("refreshToken")
        oauth_client_id = data.get("oauthClientId")
        scopes = oauth.get("scopes", [])
        subscription_type = oauth.get("subscriptionType")
        rate_limit_tier = oauth.get("rateLimitTier")

        # expiresAt is in epoch milliseconds
        expires_at = None
        raw_expires = oauth.get("expiresAt")
        if raw_expires:
            try:
                expires_at = datetime.fromtimestamp(raw_expires / 1000, tz=timezone.utc)
            except (ValueError, OSError) as exc:
                logger.warning("Failed to parse expiresAt for %s: %s", service, exc)

        token = DiscoveredToken(
            source=source,
            provider="claude",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            subscription_type=subscription_type,
            oauth_client_id=oauth_client_id,
            scopes=scopes,
        )

        logger.info(
            "Discovered Claude token from %s: plan=%s, tier=%s, expires=%s, "
            "scopes=%d, has_refresh=%s",
            service,
            subscription_type,
            rate_limit_tier,
            token.expires_in_human,
            len(scopes),
            refresh_token is not None,
        )

        discovered.append(token)

    return discovered


def discovered_token_to_token(
    discovered: DiscoveredToken,
    *,
    token_id: str,
    priority: int = 100,
) -> Token:
    """Convert a discovered token into a stored Token model."""
    scopes = " ".join(discovered.scopes) if discovered.scopes else None
    return Token(
        id=token_id,
        provider=discovered.provider,
        access_token=discovered.access_token,
        refresh_token=discovered.refresh_token,
        account_id=discovered.account_id,
        oauth_client_id=discovered.oauth_client_id,
        scopes=scopes,
        expires_at=discovered.expires_at,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Combined discovery
# ---------------------------------------------------------------------------

def discover_all_tokens() -> List[DiscoveredToken]:
    """Discover all OAuth tokens on this machine.

    Searches:
    1. ~/.codex/auth.json for OpenAI (Codex) tokens
    2. macOS Keychain for Claude Code tokens

    Returns a list of DiscoveredToken objects with masked-safe display methods.
    """
    logger.info("Starting token discovery...")

    all_tokens: List[DiscoveredToken] = []

    # Codex / OpenAI
    try:
        codex_tokens = discover_codex_tokens()
        all_tokens.extend(codex_tokens)
        logger.info("Codex discovery: found %d token(s)", len(codex_tokens))
    except Exception as exc:
        logger.error("Codex discovery failed: %s", exc)

    # Claude Code (Keychain)
    try:
        claude_tokens = discover_claude_tokens()
        all_tokens.extend(claude_tokens)
        logger.info("Claude discovery: found %d token(s)", len(claude_tokens))
    except Exception as exc:
        logger.error("Claude discovery failed: %s", exc)

    logger.info(
        "Token discovery complete: %d total (%d openai, %d claude)",
        len(all_tokens),
        sum(1 for t in all_tokens if t.provider == "openai"),
        sum(1 for t in all_tokens if t.provider == "claude"),
    )

    return all_tokens


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def print_discovery_report(tokens: List[DiscoveredToken]) -> None:
    """Print a human-readable report of discovered tokens."""
    if not tokens:
        print("No OAuth tokens found on this machine.")
        return

    print(f"\n{'='*70}")
    print(f" OAuth Token Discovery Report")
    print(f"{'='*70}\n")

    for i, token in enumerate(tokens, 1):
        status = "EXPIRED" if token.is_expired else "ACTIVE"
        status_marker = "[!]" if token.is_expired else "[+]"

        print(f"  {status_marker} Token #{i}: {token.provider.upper()}")
        print(f"      Source:       {token.source}")
        print(f"      Status:       {status}")
        print(f"      Expires:      {token.expires_in_human}")

        if token.subscription_type:
            print(f"      Plan:         {token.subscription_type}")
        if token.account_id:
            print(f"      Account:      {token.account_id}")
        if token.scopes:
            print(f"      Scopes:       {', '.join(token.scopes)}")

        print(f"      Access Token: {token.masked_token(token.access_token)}")
        print(f"      Refresh:      {'yes' if token.refresh_token else 'no'}")

        if token.error:
            print(f"      ERROR:        {token.error}")

        print()

    # Summary
    active = [t for t in tokens if not t.is_expired and not t.error]
    expired = [t for t in tokens if t.is_expired]
    errored = [t for t in tokens if t.error]

    print(f"{'─'*70}")
    print(f"  Summary: {len(active)} active, {len(expired)} expired, {len(errored)} errored")
    print(f"{'─'*70}\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    tokens = discover_all_tokens()
    print_discovery_report(tokens)
