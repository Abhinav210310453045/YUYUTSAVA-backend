"""SQL dialect adapters — the seam that lets one store serve both backends.

Phase 2 step 2.2 (ADR-002). Every persisted domain is currently written twice:
a SQLite class and a Postgres class implementing the same contract in two SQL
dialects. That is ~8,000 duplicated lines, the source of the drift the
conformance suite keeps finding, and why adding one field costs ~12 edits.

The twins are *parallel*, not divergent — same method names, same order, same
contracts — which is exactly the shape a dialect adapter collapses. What
actually differs is small and enumerable:

===================  ==========================  ================================
Difference           SQLite                      Postgres
===================  ==========================  ================================
Placeholder          ``?``                       ``%s``
Epoch timestamps     ``REAL`` column, read       ``timestamptz``, needs
                     back as-is                  ``extract(epoch FROM col)``
Row access           ``aiosqlite.Row`` mapping   tuple by default
Parent FK            no constraint               ``ensure_thread()`` before insert
Write semantics      ``BEGIN IMMEDIATE`` +       autocommit pool +
                     retry-on-busy               explicit ``transaction()``
===================  ==========================  ================================

All five are normalised here, so a domain store is written once.

Why writes take a callback
--------------------------
``write()`` takes ``fn(conn)`` rather than being an ``async with`` block, and
that is deliberate. SQLite writes retry on ``SQLITE_BUSY``, and **retrying means
re-running the body** — which a context manager fundamentally cannot do, because
its body lives in the caller's frame and executes exactly once. A callback can be
re-invoked; a ``with`` block cannot.

So ``write()`` preserves SQLite's existing retry behaviour instead of silently
dropping it, and gives Postgres a real transaction. Both roll back on failure —
proven against live servers in ``test/storage/test_rollback.py``.

Deliberately *not* an ORM. ADR-002 rejects one: the codebase is fully async over
two hand-tuned drivers with loop-affinity requirements, and pgvector search is
hand-written for good reason. This is a thin seam; raw SQL stays readable.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, TypeVar

T = TypeVar("T")


class Dialect(Protocol):
    """What a domain store needs to know about its backend, and nothing more."""

    name: str

    def ph(self, count: int = 1) -> str:
        """Placeholder list: ``ph(3)`` -> ``"?, ?, ?"`` or ``"%s, %s, %s"``."""
        ...

    def epoch(self, column: str, alias: str | None = None) -> str:
        """Select-list expression yielding *column* as a float epoch.

        Result is named *alias*, defaulting to *column*. Pass one whenever the
        column is table-qualified (``d.ts``): a qualified name is not a legal
        alias, so ``epoch("d.ts")`` alone would emit invalid SQL.

        Every Postgres timestamp column is ``TIMESTAMPTZ`` since migration v20,
        so this is always the right helper for reading one back as a float — and
        it really is a ``float``: the Postgres implementation casts, because
        ``extract(epoch ...)`` otherwise returns ``numeric``/``Decimal``.
        """
        ...

    def ts_param(self) -> str:
        """Placeholder for writing an epoch float into a **TIMESTAMPTZ** column.

        Emits ``to_timestamp(%s)`` on Postgres, so it is correct **only** where
        the Postgres column is ``TIMESTAMPTZ``. Several tables store epoch
        seconds as ``DOUBLE PRECISION`` instead (``tasks``, ``interrupts``);
        those bind with a plain :meth:`ph` on both backends, and using this
        instead raises *"column is of type double precision but expression is of
        type timestamp with time zone"*.

        The name says "timestamp" but the choice is per **column**, not per
        backend — check ``storage/pg/migrations.py`` before reaching for it.
        Same caveat applies to :meth:`epoch` on the read side.
        """
        ...

    #: Whether this backend can store and search embedding vectors.
    #:
    #: Declared, not probed. ``backfill_embeddings`` was previously discovered
    #: with ``getattr(store, "backfill_embeddings", None)`` at three separate
    #: call sites — duck typing, so a fourth call site that forgot the guard
    #: would ``AttributeError`` on SQLite only, and only in production.
    #: ``test/storage/test_twin_conformance.py`` flags that pattern by name.
    supports_vectors: bool

    def json_param(self) -> str:
        """Placeholder for writing a JSON *string* into this backend's JSON column.

        Postgres stores these as ``jsonb`` and needs an explicit ``%s::jsonb``
        cast; SQLite stores TEXT and needs nothing. Callers always pass a
        ``json.dumps`` string, so the write side looks the same either way.
        """
        ...

    def json_value(self, raw: Any) -> Any:
        """Normalise a JSON column read back from this backend.

        The asymmetry this exists for: ``jsonb`` deserialises to a ``dict``
        while TEXT comes back as a ``str``. Without normalising, the same row
        yields different Python types per backend — which is exactly how the
        twins diverged in the first place.
        """
        ...

    def reading(self) -> Any:
        """``async with`` giving a read connection whose rows are mappings."""
        ...

    async def write(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        """Run *fn* atomically. Commits on success, rolls back on any failure."""
        ...

    async def ensure_parent(self, conn: Any, thread_id: str | None, **attrs: Any) -> None:
        """Create the parent thread row if this backend has an FK requiring it.

        ``**attrs`` (``origin``, ``workspace``, ``status``, ``title``) are
        recorded only when the hub row is first created. They are carried
        through rather than dropped: a task insert is often what *creates* the
        row, and it is the only caller that knows the origin.

        No-op for a falsy ``thread_id`` — a queued task usually has no thread
        yet — and a no-op entirely on backends without the FK.
        """
        ...

    def is_unique_violation(self, exc: BaseException) -> bool:
        """True when *exc* is a duplicate-key error from this backend.

        Needed because "allocate the next number and insert it" is not
        serialisable on Postgres at READ COMMITTED: two concurrent transactions
        both read the same ``MAX(version)`` from their snapshot and both insert
        ``max + 1``, and one loses on the primary key. Being a single statement
        does not help — the ``SELECT`` still runs against a snapshot.

        SQLite does not hit this (writes serialise through one lock), so it is a
        genuine backend difference and belongs on the dialect.
        """
        ...


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteDialect:
    """Backed by a :class:`~yuyutsava.storage.base.BaseSqliteStore`.

    Delegates writes to ``_run_write``, inheriting its ``BEGIN IMMEDIATE``,
    retry-on-``SQLITE_BUSY`` and explicit rollback rather than reimplementing
    (and subtly weakening) them.
    """

    name = "sqlite"
    #: No pgvector. Semantic search degrades to keyword matching.
    supports_vectors = False

    def __init__(self, store: Any) -> None:
        self._store = store

    def ph(self, count: int = 1) -> str:
        return ", ".join(["?"] * count)

    def epoch(self, column: str, alias: str | None = None) -> str:
        # Already REAL epoch seconds; alias only when the caller needs a
        # different result name than the (possibly qualified) column.
        if alias and alias != column:
            return f"{column} AS {alias}"
        return column

    def ts_param(self) -> str:
        return "?"

    def json_param(self) -> str:
        return "?"

    def json_value(self, raw: Any) -> Any:
        """SQLite hands JSON back as TEXT — parse it."""
        if isinstance(raw, (dict, list)):
            return raw          # already parsed (a caller passed a live object)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[Any]:
        await self._store._ensure_schema()
        async with self._store._conn() as conn:
            yield conn  # row_factory is aiosqlite.Row -> mapping access

    async def write(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        return await self._store._run_write(fn)

    async def ensure_parent(self, conn: Any, thread_id: str | None, **attrs: Any) -> None:
        return None  # SQLite has no thread-hub FK

    def is_unique_violation(self, exc: BaseException) -> bool:
        import sqlite3

        return isinstance(exc, sqlite3.IntegrityError) and "unique" in str(exc).lower()


class EventsSqliteDialect(SqliteDialect):
    """SQLite dialect over :class:`~yuyutsava.storage.events.sqlite_backend.SqliteEventsBackend`.

    The events package does **not** use ``BaseSqliteStore``. It holds one
    persistent ``aiosqlite`` connection, opened once with the schema created
    eagerly at ``Store.start()``, rather than a lazy connection per call. So the
    plain :class:`SqliteDialect` — which drives ``_ensure_schema`` / ``_conn`` /
    ``_run_write`` — cannot wrap it.

    Only the two access methods differ; placeholders, epochs and unique-violation
    detection are inherited unchanged.

    This also removes a layering violation: the events twins reached into
    ``backend._write_lock`` and ``backend._c`` directly (see the pre-migration
    ``SqliteToolCounterStore.incr``). ``transaction()`` is the public equivalent
    and takes the same lock, so a transaction and a single-statement write still
    cannot interleave.
    """

    name = "sqlite"

    def __init__(self, backend: Any) -> None:  # SqliteEventsBackend
        self._b = backend

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[Any]:
        # The connection is already open and schema-migrated; reads share it
        # (aiosqlite serialises operations on its worker thread).
        yield self._b._c

    async def write(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        async with self._b.transaction() as conn:
            return await fn(conn)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class PostgresDialect:
    """Backed by a :class:`~yuyutsava.storage.pg.pool.PgPool`.

    Connections are opened with ``dict_row`` so rows are mappings, matching
    ``aiosqlite.Row``. That one choice is what lets a single row-mapper serve
    both backends — without it, every store still needs two of them, and the
    duplication this module exists to remove survives in the mapping layer.
    """

    name = "postgres"

    #: pgvector is installed by migration v8.
    supports_vectors = True

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def ph(self, count: int = 1) -> str:
        return ", ".join(["%s"] * count)

    def epoch(self, column: str, alias: str | None = None) -> str:
        # ::float8 is load-bearing. `extract(epoch FROM ...)` yields `numeric`,
        # which psycopg hands back as a `Decimal` — so without the cast a
        # timestamp reads as Decimal on Postgres and float on SQLite, and
        # `Decimal(...) == float(...)` is False. Stores that happened to wrap
        # the value in `float()` hid it; the ones that did not compared unequal
        # against their own input. Casting here fixes every caller at once.
        return f"extract(epoch FROM {column})::float8 AS {alias or column}"

    def ts_param(self) -> str:
        return "to_timestamp(%s)"

    def json_param(self) -> str:
        return "%s::jsonb"

    def json_value(self, raw: Any) -> Any:
        """psycopg already deserialised ``jsonb`` — pass it through.

        Guarded rather than assumed: a column declared TEXT on one deployment
        and ``jsonb`` on another would otherwise return two different types
        from the same code path.
        """
        if isinstance(raw, (dict, list)) or raw is None:
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    @asynccontextmanager
    async def _as_mappings(conn: Any) -> AsyncIterator[Any]:
        """Give *conn* ``dict_row`` for this block, then put it back.

        **Restoring is not optional.** ``conn.row_factory = dict_row`` mutates
        the connection, and a pooled connection is returned to the pool
        afterwards — so without the restore, every later borrower of that
        connection also gets mappings. Code reading rows positionally
        (``row[0]``) then fails with ``KeyError: 0``, at a call site that never
        touched the dialect.

        That is what happened: the daemon shares one long-lived pool between the
        dialect stores and ``PgPrefsBackend.get``, and runtime-settings loading
        started failing as soon as any unified store had borrowed a connection
        first. Every test missed it because each opens its own pool and usually
        exercises one kind of consumer, so the connection was never handed on.
        """
        from psycopg.rows import dict_row

        previous = conn.row_factory
        conn.row_factory = dict_row
        try:
            yield conn
        finally:
            conn.row_factory = previous

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as conn:
            async with self._as_mappings(conn) as c:
                yield c

    async def write(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        # transaction(), never connection(): the pool is autocommit, so
        # connection() would commit each statement independently and a failure
        # part-way through a multi-statement write would leave it half-applied.
        async with self._pool.transaction() as conn:
            async with self._as_mappings(conn) as c:
                return await fn(c)

    async def ensure_parent(self, conn: Any, thread_id: str | None, **attrs: Any) -> None:
        from yuyutsava.storage.pg.threads import ensure_thread

        await ensure_thread(conn, thread_id, **attrs)

    def is_unique_violation(self, exc: BaseException) -> bool:
        try:
            from psycopg import errors
        except ImportError:  # pragma: no cover
            return False
        return isinstance(exc, errors.UniqueViolation)


__all__ = ["Dialect", "EventsSqliteDialect", "PostgresDialect", "SqliteDialect"]
