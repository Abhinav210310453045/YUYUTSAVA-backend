"""Base class for async-sqlite stores in the storage layer.

Three production stores currently reinvent the same patterns: WAL +
busy_timeout setup, per-process write lock, retry-on-SQLITE_BUSY, and a
schema-version anchor for forward-only migrations. This class extracts
those so the per-domain stores in Step 2 only need to provide their schema
SQL and their read/write methods.

This module ships now (Step 1) so the storage package has its abstraction
in place before the moves in Step 2. No store inherits from it yet —
:class:`SqliteSessionStore`, :class:`Store`, and :class:`InterruptsStore`
keep their hand-rolled patterns until Step 2 swaps them over.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar

import aiosqlite


class BaseSqliteStore:
    """Async-sqlite store with WAL, busy_timeout, write lock, migration anchor.

    Subclasses must set :attr:`_SCHEMA_VERSION` and :attr:`_SCHEMA_SQL`, and
    may override :meth:`_migrate` to add forward-only migration steps. The
    meta table name and the version-key are also overridable via
    :attr:`_META_TABLE` and :attr:`_META_VERSION_KEY` if a subclass wants a
    different naming convention.

    Subclass example::

        class ProposalStore(BaseSqliteStore):
            _SCHEMA_VERSION = 1
            _SCHEMA_SQL = '''
                CREATE TABLE IF NOT EXISTS proposals_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS proposals (...);
            '''
            _META_TABLE = "proposals_meta"

            async def put(self, p: Proposal) -> None:
                async def _do(conn):
                    await conn.execute(
                        "INSERT INTO proposals (...) VALUES (...)", (...))
                await self._run_write(_do)
    """

    _SCHEMA_VERSION: ClassVar[int] = 1
    _SCHEMA_SQL: ClassVar[str] = ""
    _META_TABLE: ClassVar[str] = "store_meta"
    _META_VERSION_KEY: ClassVar[str] = "schema_version"

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._initialized = False

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Short-lived connection with WAL + busy_timeout configured.

        Each store call opens and closes its own connection; the WAL file
        is shared across opens so reads stay non-blocking.
        """
        await asyncio.to_thread(
            self._db_path.parent.mkdir, parents=True, exist_ok=True
        )
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()

    async def _ensure_schema(self) -> None:
        """Idempotent — runs once per instance and caches the result."""
        if self._initialized:
            return
        if not self._SCHEMA_SQL:
            raise RuntimeError(
                f"{type(self).__name__} did not define _SCHEMA_SQL"
            )
        async with self._conn() as conn:
            await conn.executescript(self._SCHEMA_SQL)
            await conn.execute(
                f"INSERT OR IGNORE INTO {self._META_TABLE}(key, value) VALUES(?, ?)",
                (self._META_VERSION_KEY, str(self._SCHEMA_VERSION)),
            )
            await conn.commit()
            await self._migrate(conn)
        self._initialized = True

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Forward-only migration hook. Default: bump the version anchor only.

        Override to add ``if current < N: ALTER ...`` blocks as the schema
        evolves. The base implementation reads the current version, writes
        the latest version back if it's stale, and commits.
        """
        cur = await conn.execute(
            f"SELECT value FROM {self._META_TABLE} WHERE key=?",
            (self._META_VERSION_KEY,),
        )
        row = await cur.fetchone()
        await cur.close()
        current = int(row[0]) if row else 0
        if current < self._SCHEMA_VERSION:
            await conn.execute(
                f"UPDATE {self._META_TABLE} SET value=? WHERE key=?",
                (str(self._SCHEMA_VERSION), self._META_VERSION_KEY),
            )
            await conn.commit()

    async def _run_write(
        self,
        fn: Callable[[aiosqlite.Connection], Awaitable[Any]],
    ) -> Any:
        """Serialize per-process writes; retry on SQLITE_BUSY up to 3x.

        ``fn`` runs inside a ``BEGIN IMMEDIATE`` transaction with the
        connection passed in; this method commits on success and rolls
        back on failure. The per-process ``asyncio.Lock`` prevents the
        retry loop from fighting itself when many tasks write concurrently.
        """
        await self._ensure_schema()
        async with self._write_lock:
            attempt = 0
            while True:
                try:
                    async with self._conn() as conn:
                        await conn.execute("BEGIN IMMEDIATE")
                        result = await fn(conn)
                        await conn.commit()
                        return result
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    attempt += 1
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(0.05 * attempt)
