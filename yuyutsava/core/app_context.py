"""``AppContext`` — the stores a cross-cutting operation touches, handed over.

ADR-003; findings ``F-S08`` and ``R-GLB-01``.

``purge_session`` touches the sessions, visuals, feedback and events stores. It
used to find them through process globals, which hid the dependency, forced
tests to set and restore globals, and — in a CLI process on Postgres — quietly
resolved the feedback store to the SQLite twin, so a CLI delete left the
session's feedback rows in Postgres (``R-BUG-19``).

Now the caller supplies every store. The daemon builds its context from the
stores it already holds; a one-shot CLI command opens one with
:func:`open_app_context`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from yuyutsava.core.stores import StoreFactory
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.routing.health import StorageHealth
from yuyutsava.storage.sessions import session_store_for


@dataclass(frozen=True)
class AppContext:
    """The store handles a caller needs, passed rather than looked up."""

    session_store: Any
    visual_store: Any
    feedback_store: Any
    #: The events store, when the caller keeps one open (the daemon does, on
    #: ``app.state.store``); absent, :meth:`events` constructs a per-call one.
    events_store: Any | None = None
    #: The Postgres pool the stores run on, when there is one.
    pg_pool: Any | None = None

    # -- lifecycle-carrying resolution --------------------------------------
    # The events Store is *constructed*, and a constructed store must be
    # started and stopped. So its resolver returns the owner too, rather than
    # leaving the caller to guess whether it may call ``stop()`` on a handle
    # someone else is still using.

    def events(self, settings: Any, *, pg_pool: Any | None = None) -> tuple[Any, bool]:
        """Return ``(events_store, caller_owns_lifecycle)``.

        When a store is supplied, it is returned with ``False``: it belongs to
        whoever passed it (the daemon keeps one open for the whole process on
        ``app.state.store``), and stopping it would break every other user.
        When none is supplied, a fresh one is constructed and ``True`` says the
        caller must ``start()``/``stop()`` it.
        """
        if self.events_store is not None:
            return self.events_store, False

        # Postgres routing needs a health monitor; without one for_backend
        # builds the SQLite twins, and a CLI purge on Postgres cleared the
        # thread's pending asks from a file they were never written to.
        health = StorageHealth(pg_pool) if pg_pool is not None else None
        return Store.for_backend(settings, pg_pool=pg_pool, health=health), True


@asynccontextmanager
async def open_app_context(settings: StorageSettings | None = None) -> AsyncIterator[AppContext]:
    """The stores of the configured backend, for one command outside the daemon.

    On Postgres a pool is opened (and migrations applied, as the CLI stack
    does) for the duration and every store shares it; on SQLite the stores are
    the state-dir twins.
    """
    settings = settings or StorageSettings.from_env()
    pool: PgPool | None = None
    if settings.is_postgres():
        pool = PgPool(settings)
        await pool.open()
    try:
        if pool is not None:
            await pg_migrations.apply(pool)
        factory = StoreFactory(settings, pg_pool=pool)
        yield AppContext(
            session_store=session_store_for(settings, pool=pool),
            visual_store=factory.visuals(),
            feedback_store=factory.feedback(),
            pg_pool=pool,
        )
    finally:
        if pool is not None:
            await pool.close()


@asynccontextmanager
async def open_events_store(settings: StorageSettings | None = None) -> AsyncIterator[Store]:
    """The configured backend's events store, for a process without the daemon.

    It holds the rows the daemon reads — preferences, consent grants, pending
    asks — so a CLI that writes a preference writes the daemon's. A bare
    ``Store()`` is SQLite-only: on the Postgres backend it wrote a file the
    daemon never reads (``R-BUG-21``).
    """
    settings = settings or StorageSettings.from_env()
    pool: PgPool | None = None
    health: StorageHealth | None = None
    if settings.is_postgres():
        pool = PgPool(settings)
        await pool.open()
    try:
        if pool is not None:
            await pg_migrations.apply(pool)
            health = StorageHealth(pool)
            store = Store.for_backend(settings, pg_pool=pool, health=health)
        else:
            store = Store()
        await store.start()
        try:
            yield store
        finally:
            await store.stop()
    finally:
        if health is not None:
            await health.stop()  # a probe started by a mid-run degrade
        if pool is not None:
            await pool.close()


__all__ = ["AppContext", "open_app_context", "open_events_store"]
