"""CLI entry point for OAuthModelRouter."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import typer

from oauthrouter.config import DB_PATH, load_config
from oauthrouter.models import Token, TokenStatus
from oauthrouter.token_store import TokenStore

app = typer.Typer(
    name="oauthrouter",
    help="Local reverse proxy for managing multiple OAuth tokens for Claude and OpenAI.",
)
token_app = typer.Typer(help="Manage OAuth tokens.")
app.add_typer(token_app, name="token")

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False) -> None:
    """Set up logging for CLI and library code."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down noisy libraries unless in debug mode
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _get_store() -> TokenStore:
    """Create a TokenStore instance pointing at the default DB path."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return TokenStore(str(DB_PATH))


def _run_async(coro):
    """Run an async function in a new event loop."""
    return asyncio.run(coro)


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Bind host (overrides config)"),
    port: Optional[int] = typer.Option(None, help="Bind port (overrides config)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start the OAuthModelRouter proxy server."""
    _configure_logging(verbose)
    import uvicorn

    config = load_config()
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    logger.info("Starting OAuthModelRouter on %s:%d", bind_host, bind_port)

    uvicorn.run(
        "oauthrouter.server:app",
        host=bind_host,
        port=bind_port,
        log_level="debug" if verbose else "info",
    )


@token_app.command("add")
def token_add(
    name: str = typer.Option(..., "--name", "-n", help="Friendly name for this token"),
    provider: str = typer.Option(
        ..., "--provider", "-p", help="Provider: 'claude' or 'openai'"
    ),
    access_token: str = typer.Option(
        ..., "--access-token", "-a", help="The OAuth access token"
    ),
    refresh_token: Optional[str] = typer.Option(
        None, "--refresh-token", "-r", help="The OAuth refresh token"
    ),
    token_endpoint: Optional[str] = typer.Option(
        None, "--token-endpoint", "-e", help="OAuth token refresh endpoint URL"
    ),
    oauth_client_id: Optional[str] = typer.Option(
        None, "--oauth-client-id", help="OAuth client_id to use for refresh"
    ),
    scopes: Optional[str] = typer.Option(
        None, "--scopes", help="Space-separated OAuth scopes to use for refresh"
    ),
    priority: int = typer.Option(
        100,
        "--priority",
        help="Selection priority. Lower values are used first.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Update an existing token with the same name.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Add or update an OAuth token in the store."""
    _configure_logging(verbose)

    token = Token(
        id=name,
        provider=provider,
        access_token=access_token,
        refresh_token=refresh_token,
        token_endpoint=token_endpoint,
        oauth_client_id=oauth_client_id,
        scopes=scopes,
        priority=priority,
    )

    async def _add():
        store = _get_store()
        await store.init_db()
        try:
            existing = await store.get_token(name)
            if existing and not force:
                typer.echo(
                    f"Token '{name}' already exists. Re-run with --force to update it.",
                    err=True,
                )
                raise typer.Exit(1)
            if existing:
                await store.remove_token(name)
                await store.add_token(token)
                typer.echo(f"Token '{name}' updated for provider '{provider}'.")
            else:
                await store.add_token(token)
                typer.echo(f"Token '{name}' added for provider '{provider}'.")
        except Exception as exc:
            typer.echo(f"Error adding token: {exc}", err=True)
            raise typer.Exit(1)
        finally:
            await store.close()

    _run_async(_add())


@token_app.command("list")
def token_list(
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Filter by provider"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List all stored tokens and their health status."""
    _configure_logging(verbose)

    async def _list():
        store = _get_store()
        await store.init_db()
        try:
            tokens = await store.list_tokens(provider)
            if not tokens:
                typer.echo("No tokens stored.")
                return

            # Table header
            typer.echo(
                f"{'NAME':<20} {'PROVIDER':<10} {'STATUS':<10} {'PRIORITY':<8} "
                f"{'EXPIRES':<20} {'LAST USED':<20}"
            )
            typer.echo("-" * 90)

            for t in tokens:
                expires = (
                    t.expires_at.strftime("%Y-%m-%d %H:%M")
                    if t.expires_at
                    else "unknown"
                )
                last_used = (
                    t.last_used_at.strftime("%Y-%m-%d %H:%M")
                    if t.last_used_at
                    else "never"
                )
                status_color = (
                    typer.style(t.status.value, fg=typer.colors.GREEN)
                    if t.status == TokenStatus.HEALTHY
                    else typer.style(t.status.value, fg=typer.colors.RED)
                )
                typer.echo(
                    f"{t.id:<20} {t.provider:<10} {status_color:<21} {t.priority:<8} "
                    f"{expires:<20} {last_used:<20}"
                )
        finally:
            await store.close()

    _run_async(_list())


@token_app.command("remove")
def token_remove(
    name: str = typer.Argument(help="Name of the token to remove"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Remove a token from the store."""
    _configure_logging(verbose)

    async def _remove():
        store = _get_store()
        await store.init_db()
        try:
            removed = await store.remove_token(name)
            if removed:
                typer.echo(f"Token '{name}' removed.")
            else:
                typer.echo(f"Token '{name}' not found.", err=True)
                raise typer.Exit(1)
        finally:
            await store.close()

    _run_async(_remove())


@token_app.command("refresh")
def token_refresh(
    name: str = typer.Argument(help="Name of the token to refresh"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Manually trigger a token refresh."""
    _configure_logging(verbose)

    async def _refresh():
        import httpx

        from oauthrouter.token_manager import TokenManager

        store = _get_store()
        await store.init_db()
        http_client = httpx.AsyncClient()
        try:
            token = await store.get_token(name)
            if not token:
                typer.echo(f"Token '{name}' not found.", err=True)
                raise typer.Exit(1)

            if not token.refresh_token:
                typer.echo(
                    f"Token '{name}' has no refresh_token — cannot refresh.",
                    err=True,
                )
                raise typer.Exit(1)

            manager = TokenManager(store, http_client)
            refreshed = await manager.refresh_token(token)
            if refreshed:
                typer.echo(
                    f"Token '{name}' refreshed successfully. "
                    f"New expiry: {refreshed.expires_at or 'unknown'}"
                )
            else:
                typer.echo(f"Token '{name}' refresh failed.", err=True)
                raise typer.Exit(1)
        finally:
            await http_client.aclose()
            await store.close()

    _run_async(_refresh())


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the health status of the router and all tokens."""
    _configure_logging(verbose)

    async def _status():
        config = load_config()
        store = _get_store()
        await store.init_db()
        try:
            typer.echo("OAuthModelRouter Status")
            typer.echo("=" * 40)
            typer.echo(
                f"Config: server={config.server.host}:{config.server.port}"
            )
            typer.echo(f"Providers: {', '.join(config.providers.keys())}")
            typer.echo()

            for provider_name, provider_cfg in config.providers.items():
                tokens = await store.list_tokens(provider_name)
                healthy = [t for t in tokens if t.status == TokenStatus.HEALTHY]
                typer.echo(
                    f"  {provider_name}: {len(healthy)}/{len(tokens)} healthy "
                    f"→ {provider_cfg.upstream}"
                )
                for t in tokens:
                    status_indicator = "  ✓" if t.status == TokenStatus.HEALTHY else "  ✗"
                    typer.echo(f"    {status_indicator} {t.id} ({t.status.value})")
        finally:
            await store.close()

    _run_async(_status())


if __name__ == "__main__":
    app()
