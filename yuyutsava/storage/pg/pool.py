"""Async Postgres connection pool — lifecycle owner for the daemon/CLI.

One :class:`PgPool` per process, opened in ``daemon/bootstrap.py`` before the
checkpointer and closed during ordered teardown (it sits on
``DaemonSubsystems``). Stores that need Postgres (artifacts, summaries,
memories) borrow connections via :meth:`PgPool.connection`; none of them own
connection lifecycle themselves.

The LangGraph ``AsyncPostgresSaver`` keeps its *own* connection (its
``from_conn_string`` context manager) — sharing a pool with the saver buys
nothing and couples our teardown order to its internals.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg_pool import AsyncConnectionPool

from yuyutsava.storage.backend import StorageSettings

logger = logging.getLogger("yuyutsava.storage.pg.pool")


class PgPool:
    """Owns an ``AsyncConnectionPool``; open at boot, close at shutdown."""

    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool | None = None

    async def open(self, *, timeout_sec: float = 10.0) -> None:
        """Open the pool and verify connectivity. Raises on failure.

        ``wait=True`` forces at least ``min_size`` connections to be
        established before returning, so a dead Postgres surfaces here —
        at boot, where the caller can decide to fall back — rather than on
        the first query mid-task.
        """
        if self._pool is not None:
            return
        pool = AsyncConnectionPool(
            conninfo=self._settings.pg_dsn,
            min_size=self._settings.pool_min,
            max_size=self._settings.pool_max,
            open=False,
            kwargs={"autocommit": True},
        )
        await pool.open(wait=True, timeout=timeout_sec)
        self._pool = pool
        logger.info("pg pool: open (min=%d max=%d)",
                    self._settings.pool_min, self._settings.pool_max)

    async def close(self) -> None:
        """Close the pool. Idempotent."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        logger.info("pg pool: closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Borrow a pooled connection (autocommit). Use per call, not long-held."""
        if self._pool is None:
            raise RuntimeError("PgPool.open() must be called first")
        async with self._pool.connection() as conn:
            yield conn

    @property
    def dsn(self) -> str:
        return self._settings.pg_dsn
