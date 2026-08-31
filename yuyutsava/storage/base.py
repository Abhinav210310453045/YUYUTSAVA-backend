"""Base class for async-sqlite stores in the storage layer.

Centralises the patterns every SQLite store needs: WAL + busy_timeout setup,
a per-process write lock, retry-on-SQLITE_BUSY, and a schema-version anchor
for forward-only migrations. Subclasses supply only their schema SQL and
their read/write methods.

**This class is load-bearing.** It is the base of the SQLite half of ~14
domain stores, including the artifact, summary, transcript, memory, skill,
todo, visual, voice, feedback, interrupt, task, usage and session stores.
Changing its transaction or retry behaviour changes all of them at once.

Note that only the *SQLite* twins inherit from it — the Postgres twins reach
the pool directly and therefore do NOT get its ``BEGIN IMMEDIATE`` wrapper or
its retry policy. That asymmetry is a known LSP defect (finding ``F-S10`` in
docs/architecture-review/); it is resolved by ADR-002, which moves the
transaction policy into a shared dialect adapter.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Iterator

import aiosqlite

from yuyutsava.platform import FileLock
from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.storage.base")


def _migrations_lock_path() -> Path:
    return state_dir() / "migrations.lock"


@contextmanager
def migration_lock() -> Iterator[None]:
    """Cross-process exclusive lock for schema migrations.

    SQLite WAL + ``busy_timeout`` already serializes normal CRUD writers
    across processes. The remaining hole is concurrent schema migrations
    on first daemon + chat startup: each process can race
    ``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE`` calls. Wrap the
    migration block with this and only one process will run migrations
    at a time; the other waits.

    Blocking lock — every caller will eventually get it. The body is
    expected to be fast (milliseconds) so blocking is fine. Cross-platform
    via :class:`yuyutsava.platform.FileLock` (portalocker under the hood).
    """
    with FileLock(_migrations_lock_path()):
        yield


@asynccontextmanager
async def amigration_lock() -> AsyncIterator[None]:
    """Async wrapper around :func:`migration_lock` that runs the blocking
    lock acquisition on a worker thread so the asyncio loop stays responsive.
    """
    lock = FileLock(_migrations_lock_path())
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)


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
        """Idempotent — runs once per instance and caches the result.

        Wrapped in the cross-process migration lock so a second daemon /
        chat starting at the same time can't race on schema setup.
        """
        if self._initialized:
            return
        if not self._SCHEMA_SQL:
            raise RuntimeError(
                f"{type(self).__name__} did not define _SCHEMA_SQL"
            )
        async with amigration_lock():
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
        connection passed in; this method commits on success and **explicitly
        rolls back** on failure, so a partially-applied write is never left
        behind. The per-process ``asyncio.Lock`` prevents the retry loop from
        fighting itself when many tasks write concurrently.

        The rollback used to be implicit — the connection was simply closed and
        SQLite discarded the open transaction. That worked, but it relied on
        driver close semantics rather than stating the intent, and it left the
        retry path re-entering with the previous attempt's partial work only
        *probably* gone. It is explicit now.
        """
        await self._ensure_schema()
        async with self._write_lock:
            attempt = 0
            while True:
                try:
                    async with self._conn() as conn:
                        await conn.execute("BEGIN IMMEDIATE")
                        try:
                            result = await fn(conn)
                        except BaseException:
                            # Roll back before the connection closes, so the
                            # transaction is discarded deliberately rather than
                            # by side effect. BaseException so that a cancelled
                            # task cannot leave a write half-applied either.
                            await conn.rollback()
                            raise
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
