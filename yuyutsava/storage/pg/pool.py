"""Async Postgres connection pool — lifecycle owner for the daemon/CLI.

One :class:`PgPool` per process, opened in ``daemon/bootstrap.py`` before the
checkpointer and closed during ordered teardown (it sits on
``DaemonSubsystems``). Stores that need Postgres (artifacts, summaries,
memories) borrow connections via :meth:`PgPool.connection`; none of them own
connection lifecycle themselves.

One *underlying* ``AsyncConnectionPool`` per event loop: psycopg pools are
loop-affine (their internal locks/waiters bind to the loop that opened them),
and the stores holding this ``PgPool`` are also awaited from the
AsyncSubagentHost's uvicorn loop (background subagent middleware). ``open()``
opens the primary pool on the boot loop; any other loop that borrows a
connection gets a lazily-opened secondary pool (``min_size=0``, grows on
demand). ``close()`` closes the current loop's pool — a pool cannot be closed
from a foreign loop, so secondary pools on other loops die with the process
(the host thread is a daemon thread; same teardown semantics as
``AsyncSubagentHost.shutdown``). See Architecture.md "Event-loop ownership".

The LangGraph ``AsyncPostgresSaver`` keeps its *own* connection (its
``from_conn_string`` context manager) — sharing a pool with the saver buys
nothing and couples our teardown order to its internals.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg_pool import AsyncConnectionPool

from yuyutsava.aio import LoopLocal
from yuyutsava.storage.backend import StorageSettings

logger = logging.getLogger("yuyutsava.storage.pg.pool")


class PgPool:
    """Owns per-loop ``AsyncConnectionPool``s; open at boot, close at shutdown."""

    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._pools: LoopLocal[AsyncConnectionPool] = LoopLocal()
        # Set by open(): PG is reachable and pools may sprout on other loops.
        # A PG-disabled boot must keep raising, not silently open pools.
        self._opened = False

    async def open(self, *, timeout_sec: float = 10.0) -> None:
        """Open the primary pool and verify connectivity. Raises on failure.

        ``wait=True`` forces at least ``min_size`` connections to be
        established before returning, so a dead Postgres surfaces here —
        at boot, where the caller can decide to fall back — rather than on
        the first query mid-task.
        """
        if self._opened:
            return

        async def _open_primary() -> AsyncConnectionPool:
            pool = AsyncConnectionPool(
                conninfo=self._settings.pg_dsn,
                min_size=self._settings.pool_min,
                max_size=self._settings.pool_max,
                open=False,
                kwargs={"autocommit": True},
            )
            await pool.open(wait=True, timeout=timeout_sec)
            return pool

        await self._pools.aget(_open_primary)
        self._opened = True
        logger.info("pg pool: open (min=%d max=%d)",
                    self._settings.pool_min, self._settings.pool_max)

    async def _pool(self) -> AsyncConnectionPool:
        """The current loop's pool, opening a secondary one on first use.

        Secondary pools start empty (``min_size=0``) so a mostly-idle loop
        costs no connections; worst case across loops is ``pool_max`` each.
        """
        if not self._opened:
            raise RuntimeError("PgPool.open() must be called first")

        async def _open_secondary() -> AsyncConnectionPool:
            pool = AsyncConnectionPool(
                conninfo=self._settings.pg_dsn,
                min_size=0,
                max_size=self._settings.pool_max,
                open=False,
                kwargs={"autocommit": True},
            )
            await pool.open(wait=False)
            logger.info(
                "pg pool: secondary pool for loop on thread %r (max=%d)",
                threading.current_thread().name, self._settings.pool_max,
            )
            return pool

        return await self._pools.aget(_open_secondary)

    async def close(self) -> None:
        """Close the current loop's pool and mark the whole PgPool closed.
        Idempotent.

        Pools opened by other loops cannot be closed from here (closing is
        itself loop-affine); they die with the process.
        """
        if not self._opened:
            return
        self._opened = False
        pool = self._pools.pop_current()
        if pool is not None:
            await pool.close()
        logger.info("pg pool: closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Borrow a pooled connection (autocommit). Use per call, not long-held."""
        pool = await self._pool()
        async with pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Borrow a connection wrapped in an explicit ``BEGIN``/``COMMIT`` block.

        The pool is autocommit, so single statements commit on their own. Use
        this for multi-statement writes that must be atomic (dedup-probe +
        insert, proposal → decision sequences): psycopg's ``transaction()``
        issues ``BEGIN`` on entry and ``COMMIT`` on clean exit / ``ROLLBACK``
        if the body raises, even on an autocommit connection. Scope it tightly
        with ``async with`` — a held-open transaction pins a pool connection.
        """
        pool = await self._pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                yield conn

    @property
    def dsn(self) -> str:
        return self._settings.pg_dsn
