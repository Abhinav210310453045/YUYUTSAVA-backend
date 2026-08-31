"""One ``VisualStore`` implementation over both backends — the ADR-002 proof.

Phase 2 step 2.3. Visuals were chosen as the first migration for the reasons
ADR-002 gives: small (two twins, ~210 lines together), already wrapped in
``RoutedStore``, and carrying an **on-disk side effect** (the row holds an
absolute path to a PNG) so any hidden assumption surfaces immediately rather
than in a domain where a raw table delete would look fine.

What this replaces
------------------
``SqliteVisualStore`` (126 lines) + ``PgVisualStore`` (85 lines) = 211 lines of
parallel implementation. Below is one implementation of the same six methods,
parameterised by a :class:`~yuyutsava.storage.dialect.Dialect`.

The business rules are stated **once**:

* the image file is written before the row, so a row never points at a missing
  file;
* ``delete``/``delete_for_thread``/``delete_older_than`` read the paths first,
  delete the rows, then unlink — a raw table delete would orphan the PNGs;
* the parent thread row is ensured before insert on backends with the FK.

Schema still lives per-backend (SQLite ``_SCHEMA_SQL``, Postgres migrations) —
unifying DDL is a separate step and is not required for the read/write paths to
collapse.

This module ships **alongside** the twins, not instead of them. The switchover
happens once ``test/storage/test_visual_store_parity.py`` shows identical
behaviour against both live backends; keeping both available is what makes the
migration reversible.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import ClassVar

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect
from .store import (
    DEFAULT_LIST_LIMIT,
    _EXT,
    VisualRecord,
    VisualStore,
    _blob_dir,
    _unlink_all,
    _write_file,
)
from .types import RenderResult

logger = logging.getLogger("yuyutsava.visuals.store_unified")

#: Explicit column list. ``SELECT *`` cannot be shared: Postgres needs
#: ``extract(epoch FROM created_ts)`` to return a float, so the timestamp column
#: is rendered by the dialect and the rest are named.
_COLS = ("visual_id", "thread_id", "kind", "title", "mime", "path", "source")


def _row_to_record(row) -> VisualRecord:
    """One mapper for both backends.

    Works because ``PostgresDialect`` opens connections with ``dict_row``, so a
    Postgres row is a mapping exactly like ``aiosqlite.Row``. Without that, this
    function would need two versions and the duplication would simply move here.
    """
    return VisualRecord(
        visual_id=row["visual_id"],
        thread_id=row["thread_id"],
        kind=row["kind"],
        title=row["title"],
        mime=row["mime"],
        path=row["path"],
        source=row["source"],
        created_ts=float(row["created_ts"]),
    )


class VisualSchema(BaseSqliteStore):
    """Schema owner for the SQLite side — no query methods, only DDL.

    ``SqliteDialect`` needs a ``BaseSqliteStore`` for its connection, schema
    bootstrap and ``_run_write`` machinery. Handing it the old
    ``SqliteVisualStore`` would work but would keep the 126-line twin alive
    purely for plumbing, which defeats the point of the collapse.

    Separating schema ownership from query behaviour is also the right shape for
    the remaining domains: the DDL is genuinely backend-specific (Postgres owns
    its schema in ``pg/migrations.py``), while the queries are not.

    Values match the original ``SqliteVisualStore`` exactly, so an existing
    ``state.db`` is picked up unchanged — no migration, no version bump.
    """

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "visual_artifacts_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS visual_artifacts_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visual_artifacts (
            visual_id   TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            title       TEXT,
            mime        TEXT NOT NULL,
            path        TEXT NOT NULL,
            source      TEXT,
            created_ts  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS visual_artifacts_thread_idx
            ON visual_artifacts (thread_id, created_ts);
    """


def sqlite_visual_store(db_path: Path | None = None) -> "UnifiedVisualStore":
    """SQLite-backed visual store, schema included."""
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedVisualStore(SqliteDialect(VisualSchema(db_path or state_db_path())))


def pg_visual_store(pool) -> "UnifiedVisualStore":
    """Postgres-backed visual store (schema owned by pg/migrations v14)."""
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedVisualStore(PostgresDialect(pool))


class UnifiedVisualStore(VisualStore):
    """``visual_artifacts`` on whichever backend the dialect wraps."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    def _select(self) -> str:
        cols = ", ".join(_COLS)
        return f"SELECT {cols}, {self._d.epoch('created_ts')} FROM visual_artifacts"

    async def save(
        self, result: RenderResult, thread_id: str, *, out_dir: str | Path | None = None
    ) -> VisualRecord:
        visual_id = f"vis_{ULID()}"
        path = _blob_dir(out_dir) / f"{visual_id}.{_EXT.get(result.mime, 'png')}"
        # File first: a row must never point at a file that does not exist.
        await asyncio.to_thread(_write_file, path, result.image_bytes)

        rec = VisualRecord(
            visual_id=visual_id, thread_id=thread_id, kind=result.kind,
            title=result.title, mime=result.mime, path=str(path),
            source=result.source, created_ts=time.time(),
        )
        d = self._d

        async def _do(conn):
            await d.ensure_parent(conn, thread_id)
            await conn.execute(
                f"INSERT INTO visual_artifacts "
                f"({', '.join(_COLS)}, created_ts) "
                f"VALUES ({d.ph(len(_COLS))}, {d.ts_param()})",
                (rec.visual_id, rec.thread_id, rec.kind, rec.title, rec.mime,
                 rec.path, rec.source, rec.created_ts),
            )

        await d.write(_do)
        return rec

    async def get(self, visual_id: str) -> VisualRecord | None:
        async with self._d.reading() as conn:
            cur = await conn.execute(
                f"{self._select()} WHERE visual_id = {self._d.ph()}", (visual_id,)
            )
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[VisualRecord]:
        async with self._d.reading() as conn:
            cur = await conn.execute(
                f"{self._select()} WHERE thread_id = {self._d.ph()} "
                f"ORDER BY created_ts DESC LIMIT {self._d.ph()}",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def _paths_then_delete(self, where: str, params: tuple) -> int:
        """Read paths, delete rows atomically, then unlink the files.

        Order matters and is the whole reason visuals cannot be purged by a raw
        table delete: the row is the only record of where the PNG lives, so the
        paths must be read before the rows go, and the files unlinked only after
        the delete commits.
        """
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"SELECT path FROM visual_artifacts WHERE {where}", params
            )
            paths = [r["path"] for r in await cur.fetchall()]
            await conn.execute(f"DELETE FROM visual_artifacts WHERE {where}", params)
            return paths

        paths = await d.write(_do)
        if paths:
            await asyncio.to_thread(_unlink_all, paths)
        return len(paths)

    async def delete(self, visual_id: str) -> bool:
        return await self._paths_then_delete(
            f"visual_id = {self._d.ph()}", (visual_id,)
        ) > 0

    async def delete_for_thread(self, thread_id: str) -> int:
        return await self._paths_then_delete(
            f"thread_id = {self._d.ph()}", (thread_id,)
        )

    async def delete_older_than(self, cutoff_ts: float) -> int:
        return await self._paths_then_delete(
            f"created_ts < {self._d.ts_param()}", (cutoff_ts,)
        )


__all__ = [
    "UnifiedVisualStore", "VisualSchema",
    "pg_visual_store", "sqlite_visual_store",
]
