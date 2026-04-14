"""Configuration loading for OAuthModelRouter."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

from oauthrouter.models import AppConfig, ProviderConfig, ServerConfig

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".oauthrouter"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DB_PATH = CONFIG_DIR / "tokens.db"

DEFAULT_PROVIDERS = {
    "claude": ProviderConfig(
        upstream="https://api.anthropic.com",
        auth_header="Authorization",
        auth_prefix="Bearer",
        token_endpoint="https://platform.claude.com/v1/oauth/token",
        oauth_client_id="https://claude.ai/oauth/claude-code-client-metadata",
        extra_headers={"anthropic-beta": "oauth-2025-04-20"},
    ),
    "openai": ProviderConfig(
        upstream="https://api.openai.com",
        auth_header="Authorization",
        auth_prefix="Bearer",
        token_endpoint="https://auth.openai.com/oauth/token",
        oauth_client_id=None,
    ),
}


def _default_config_dict() -> dict:
    """Return the default config as a plain dict for TOML serialization."""
    return {
        "server": {"host": "127.0.0.1", "port": 8000},
        "providers": {
            name: {
                k: v
                for k, v in cfg.model_dump().items()
                if v is not None
            }
            for name, cfg in DEFAULT_PROVIDERS.items()
        },
    }


def _ensure_config_exists() -> None:
    """Create the default config file if it doesn't exist."""
    if CONFIG_PATH.exists():
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _default_config_dict()
    CONFIG_PATH.write_bytes(tomli_w.dumps(data).encode())
    logger.info("Created default config at %s", CONFIG_PATH)


def save_config(config: AppConfig) -> None:
    """Persist the current configuration to ~/.oauthrouter/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "server": config.server.model_dump(),
        "providers": {
            name: {
                key: value
                for key, value in provider.model_dump().items()
                if value is not None
            }
            for name, provider in config.providers.items()
        },
    }
    CONFIG_PATH.write_bytes(tomli_w.dumps(data).encode())
    logger.info("Config saved to %s", CONFIG_PATH)


def load_config() -> AppConfig:
    """Load configuration from ~/.oauthrouter/config.toml.

    Creates a default config file on first run. Merges file values on top of
    built-in defaults so the user only needs to specify overrides.
    """
    _ensure_config_exists()

    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)

    logger.debug("Raw config loaded from %s: %s", CONFIG_PATH, raw)

    server = ServerConfig(**raw.get("server", {}))

    providers: dict[str, ProviderConfig] = {}
    for name, cfg in DEFAULT_PROVIDERS.items():
        overrides = raw.get("providers", {}).get(name, {})
        merged = {**cfg.model_dump(), **overrides}
        providers[name] = ProviderConfig(**{k: v for k, v in merged.items() if v is not None})

    # Allow additional providers defined in config but not in defaults
    for name, overrides in raw.get("providers", {}).items():
        if name not in providers:
            providers[name] = ProviderConfig(**overrides)
            logger.info("Loaded custom provider %r from config", name)

    config = AppConfig(server=server, providers=providers)
    logger.info(
        "Config loaded: server=%s:%d, providers=%s",
        config.server.host,
        config.server.port,
        list(config.providers.keys()),
    )
    return config
