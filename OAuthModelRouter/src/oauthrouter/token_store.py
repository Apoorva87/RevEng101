"""SQLite-backed token storage for OAuthModelRouter."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import aiosqlite

from oauthrouter.models import Token, TokenStatus

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_endpoint TEXT,
    account_id TEXT,
    oauth_client_id TEXT,
    scopes TEXT,
    expires_at TEXT,
    status TEXT DEFAULT 'healthy',
    priority INTEGER DEFAULT 100,
    last_used_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

MIGRATE_SQL = [
    "ALTER TABLE tokens ADD COLUMN account_id TEXT",
    "ALTER TABLE tokens ADD COLUMN oauth_client_id TEXT",
    "ALTER TABLE tokens ADD COLUMN scopes TEXT",
    "ALTER TABLE tokens ADD COLUMN priority INTEGER DEFAULT 100",
]


class TokenStore:
    """Async SQLite CRUD for OAuth tokens."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """Open the database and create the tokens table if needed."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(CREATE_TABLE_SQL)
        # Run migrations for existing databases
        for sql in MIGRATE_SQL:
            try:
                await self._db.execute(sql)
            except Exception:
                pass  # Column already exists
        await self._repair_legacy_statuses()
        await self._db.commit()
        logger.info("Token store initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            logger.debug("Token store closed")

    def _row_to_token(self, row: aiosqlite.Row) -> Token:
        """Convert a database row to a Token model."""
        d = dict(row)
        for field in ("expires_at", "last_used_at", "created_at"):
            if d.get(field):
                d[field] = datetime.fromisoformat(d[field])
        d["status"] = self._normalize_status(d.get("status"))
        return Token(**d)

    async def _repair_legacy_statuses(self) -> None:
        """Normalize old helper-script statuses into the app's DB model.

        The router only persists healthy/unhealthy. Older local helpers also
        wrote transient or unsupported values such as "rate_limited" and
        "error", which can otherwise break reads or leave tokens stuck in a
        stale state.
        """
        assert self._db is not None

        repairs = [
            (
                "UPDATE tokens SET status = ? WHERE LOWER(COALESCE(status, '')) = ?",
                (TokenStatus.HEALTHY.value, "rate_limited"),
            ),
            (
                "UPDATE tokens SET status = ? WHERE LOWER(COALESCE(status, '')) = ?",
                (TokenStatus.UNHEALTHY.value, "error"),
            ),
            (
                "UPDATE tokens SET status = ? "
                "WHERE TRIM(COALESCE(status, '')) = '' "
                "OR LOWER(status) NOT IN (?, ?)",
                (
                    TokenStatus.UNHEALTHY.value,
                    TokenStatus.HEALTHY.value,
                    TokenStatus.UNHEALTHY.value,
                ),
            ),
        ]

        repaired_rows = 0
        for sql, params in repairs:
            cursor = await self._db.execute(sql, params)
            if cursor.rowcount and cursor.rowcount > 0:
                repaired_rows += cursor.rowcount

        if repaired_rows:
            logger.info(
                "Normalized %d legacy token status row(s) in %s",
                repaired_rows,
                self._db_path,
            )

    def _normalize_status(self, raw_status: object) -> TokenStatus:
        """Map any legacy or unknown stored status into a supported enum."""
        status_text = str(raw_status or "").strip().lower()
        if status_text == TokenStatus.HEALTHY.value:
            return TokenStatus.HEALTHY
        if status_text == "rate_limited":
            logger.warning(
                "Token store read legacy status %r from %s; treating it as healthy",
                raw_status,
                self._db_path,
            )
            return TokenStatus.HEALTHY
        if status_text in (TokenStatus.UNHEALTHY.value, "error", ""):
            if status_text == "error":
                logger.warning(
                    "Token store read legacy status %r from %s; treating it as unhealthy",
                    raw_status,
                    self._db_path,
                )
            return TokenStatus.UNHEALTHY

        logger.warning(
            "Token store read unknown status %r from %s; treating it as unhealthy",
            raw_status,
            self._db_path,
        )
        return TokenStatus.UNHEALTHY

    async def add_token(self, token: Token) -> None:
        """Insert a new token into the store."""
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO tokens (id, provider, access_token, refresh_token,
                                token_endpoint, account_id, oauth_client_id, scopes,
                                expires_at, status, priority, last_used_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token.id,
                token.provider,
                token.access_token,
                token.refresh_token,
                token.token_endpoint,
                token.account_id,
                token.oauth_client_id,
                token.scopes,
                token.expires_at.isoformat() if token.expires_at else None,
                token.status.value,
                token.priority,
                token.last_used_at.isoformat() if token.last_used_at else None,
                token.created_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.info(
            "Token added: name=%s provider=%s has_refresh=%s",
            token.id,
            token.provider,
            token.refresh_token is not None,
        )

    async def get_token(self, token_id: str) -> Optional[Token]:
        """Retrieve a token by its ID."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM tokens WHERE id = ?", (token_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            logger.debug("Token not found: %s", token_id)
            return None
        return self._row_to_token(row)

    async def list_tokens(self, provider: Optional[str] = None) -> list[Token]:
        """List all tokens, optionally filtered by provider."""
        assert self._db is not None
        if provider:
            cursor = await self._db.execute(
                """
                SELECT * FROM tokens
                WHERE provider = ?
                ORDER BY priority ASC, created_at ASC, id ASC
                """,
                (provider,),
            )
        else:
            cursor = await self._db.execute(
                """
                SELECT * FROM tokens
                ORDER BY provider, priority ASC, created_at ASC, id ASC
                """
            )
        rows = await cursor.fetchall()
        return [self._row_to_token(row) for row in rows]

    async def remove_token(self, token_id: str) -> bool:
        """Remove a token by its ID. Returns True if a token was actually deleted."""
        assert self._db is not None
        cursor = await self._db.execute(
            "DELETE FROM tokens WHERE id = ?", (token_id,)
        )
        await self._db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Token removed: %s", token_id)
        else:
            logger.warning("Token remove requested but not found: %s", token_id)
        return deleted

    async def update_token(
        self,
        token_id: str,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        account_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        status: Optional[TokenStatus] = None,
        priority: Optional[int] = None,
    ) -> None:
        """Update specific fields of a token."""
        assert self._db is not None
        updates: list[str] = []
        values: list = []

        if access_token is not None:
            updates.append("access_token = ?")
            values.append(access_token)
        if refresh_token is not None:
            updates.append("refresh_token = ?")
            values.append(refresh_token)
        if account_id is not None:
            updates.append("account_id = ?")
            values.append(account_id)
        if expires_at is not None:
            updates.append("expires_at = ?")
            values.append(expires_at.isoformat())
        if status is not None:
            updates.append("status = ?")
            values.append(status.value)
        if priority is not None:
            updates.append("priority = ?")
            values.append(priority)

        if not updates:
            return

        values.append(token_id)
        sql = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
        await self._db.execute(sql, values)
        await self._db.commit()
        logger.debug(
            "Token updated: %s fields=%s",
            token_id,
            [u.split(" =")[0] for u in updates],
        )

    async def rename_token(self, old_id: str, new_id: str) -> bool:
        """Rename a token by changing its ID. Returns True if successful."""
        assert self._db is not None
        # Check the new ID isn't already taken
        cursor = await self._db.execute(
            "SELECT 1 FROM tokens WHERE id = ?", (new_id,)
        )
        if await cursor.fetchone():
            return False
        await self._db.execute(
            "UPDATE tokens SET id = ? WHERE id = ?", (new_id, old_id)
        )
        await self._db.commit()
        logger.info("Token renamed: %s -> %s", old_id, new_id)
        return True

    async def get_healthy_tokens(self, provider: str) -> list[Token]:
        """Get healthy tokens for a provider, ordered by explicit priority.

        Lower priority values are selected first. last_used_at is intentionally
        not part of routing order; it is only operational metadata.
        """
        assert self._db is not None
        cursor = await self._db.execute(
            """
            SELECT * FROM tokens
            WHERE provider = ? AND status = 'healthy'
            ORDER BY priority ASC, created_at ASC, id ASC
            """,
            (provider,),
        )
        rows = await cursor.fetchall()
        tokens = [self._row_to_token(row) for row in rows]
        logger.debug(
            "Healthy tokens for %s: %d found (names=%s)",
            provider,
            len(tokens),
            [t.id for t in tokens],
        )
        return tokens

    async def mark_unhealthy(self, token_id: str) -> None:
        """Mark a token as unhealthy."""
        await self.update_token(token_id, status=TokenStatus.UNHEALTHY)
        logger.warning("Token marked UNHEALTHY: %s", token_id)

    async def mark_healthy(self, token_id: str) -> None:
        """Mark a token as healthy (e.g. after successful refresh)."""
        await self.update_token(token_id, status=TokenStatus.HEALTHY)
        logger.info("Token marked HEALTHY: %s", token_id)

    async def mark_used(self, token_id: str) -> None:
        """Update the last_used_at timestamp for a token."""
        assert self._db is not None
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            "UPDATE tokens SET last_used_at = ? WHERE id = ?",
            (now, token_id),
        )
        await self._db.commit()
        logger.debug("Token used: %s at %s", token_id, now)
