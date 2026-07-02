"""Persistence for rendered visuals: image bytes on disk + metadata row.

Mirrors the voice-audio convention (:mod:`yuyutsava.storage.voice_store`): the
PNG lives on disk and the DB row holds its absolute path, so the HTTP layer can
serve it by id regardless of where it was written. Two write locations:

  * a tool call passes the agent's ``OUTPUT_DIR`` so the file lands in the user's
    workspace (``_output/visuals/…``) and the CLI can point at it;
  * the REST endpoint passes nothing, so it falls back to the canonical blob dir
    (:func:`yuyutsava.storage.paths.blobs_dir` / ``visuals``).

Retention: visuals are session-scoped user output. ``delete_for_thread`` runs on
session delete; ``delete_older_than`` lets the TTL sweeper age out orphans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.paths import blobs_dir
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread
from .types import RenderResult

logger = logging.getLogger("yuyutsava.visuals.store")

DEFAULT_LIST_LIMIT = 500
_EXT = {"image/png": "png", "image/svg+xml": "svg"}


@dataclass(frozen=True)
class VisualRecord:
    """One persisted visual's metadata (no image bytes — those live on disk)."""

    visual_id: str
    thread_id: str
    kind: str
    title: str | None
    mime: str
    path: str
    source: str | None
    created_ts: float


class VisualStore(ABC):
    """Interface the delivery layer depends on."""

    @abstractmethod
    async def save(
        self, result: RenderResult, thread_id: str, *, out_dir: str | Path | None = None
    ) -> VisualRecord:
        """Write the image to disk and record its metadata. Returns the record."""

    @abstractmethod
    async def get(self, visual_id: str) -> VisualRecord | None:
        """One record by id — used to serve its image file."""

    @abstractmethod
    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[VisualRecord]:
        """Records for a thread, newest first."""

    @abstractmethod
    async def delete(self, visual_id: str) -> bool:
        """Delete one visual everywhere the agent stored it — the metadata row
        and the image file on disk (``rec.path``). A user's own downloaded copy
        lives at a separate, untracked path and is untouched. Returns ``True``
        when a row was removed, ``False`` if the id was unknown."""

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop rows + image files for a thread. Returns rows deleted."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """Drop rows + files older than *cutoff_ts* (TTL sweep)."""


def _blob_dir(out_dir: str | Path | None) -> Path:
    return Path(out_dir) / "visuals" if out_dir else blobs_dir() / "visuals"


class SqliteVisualStore(BaseSqliteStore, VisualStore):
    """``visual_artifacts`` table inside ``state.db`` (zero-config)."""

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

    async def save(
        self, result: RenderResult, thread_id: str, *, out_dir: str | Path | None = None
    ) -> VisualRecord:
        visual_id = f"vis_{ULID()}"
        ext = _EXT.get(result.mime, "png")
        directory = _blob_dir(out_dir)
        path = directory / f"{visual_id}.{ext}"
        await asyncio.to_thread(_write_file, path, result.image_bytes)

        rec = VisualRecord(
            visual_id=visual_id,
            thread_id=thread_id,
            kind=result.kind,
            title=result.title,
            mime=result.mime,
            path=str(path),
            source=result.source,
            created_ts=time.time(),
        )

        async def _do(conn):
            await conn.execute(
                "INSERT INTO visual_artifacts "
                "(visual_id, thread_id, kind, title, mime, path, source, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.visual_id, rec.thread_id, rec.kind, rec.title, rec.mime,
                 rec.path, rec.source, rec.created_ts),
            )

        await self._run_write(_do)
        return rec

    async def get(self, visual_id: str) -> VisualRecord | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM visual_artifacts WHERE visual_id = ?", (visual_id,)
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_rec(row) if row else None

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[VisualRecord]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM visual_artifacts WHERE thread_id = ? "
                "ORDER BY created_ts DESC LIMIT ?",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_rec(r) for r in rows]

    async def delete(self, visual_id: str) -> bool:
        rec = await self.get(visual_id)
        if rec is None:
            return False

        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE visual_id = ?", (visual_id,)
            )
            return cur.rowcount or 0

        deleted = await self._run_write(_do)
        await asyncio.to_thread(_unlink_all, [rec.path])
        return deleted > 0

    async def delete_for_thread(self, thread_id: str) -> int:
        paths = [r.path for r in await self.list_for_thread(thread_id, limit=100_000)]

        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE thread_id = ?", (thread_id,)
            )
            return cur.rowcount or 0

        deleted = await self._run_write(_do)
        await asyncio.to_thread(_unlink_all, paths)
        return deleted

    async def delete_older_than(self, cutoff_ts: float) -> int:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT path FROM visual_artifacts WHERE created_ts < ?", (cutoff_ts,)
            )
            paths = [r["path"] for r in await cur.fetchall()]
            await cur.close()

        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE created_ts < ?", (cutoff_ts,)
            )
            return cur.rowcount or 0

        deleted = await self._run_write(_do)
        await asyncio.to_thread(_unlink_all, paths)
        return deleted


class PgVisualStore(VisualStore):
    """``visual_artifacts`` table in Postgres (schema owned by pg/migrations v14).

    Postgres is primary on the ``postgres`` backend; image bytes still live on
    disk (only the metadata index is in PG). Mirrors :class:`PgVoiceMessageStore`.
    """

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def save(
        self, result: RenderResult, thread_id: str, *, out_dir: str | Path | None = None
    ) -> VisualRecord:
        visual_id = f"vis_{ULID()}"
        ext = _EXT.get(result.mime, "png")
        path = _blob_dir(out_dir) / f"{visual_id}.{ext}"
        await asyncio.to_thread(_write_file, path, result.image_bytes)
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # satisfy visual_artifacts_thread_fk
            await conn.execute(
                "INSERT INTO visual_artifacts "
                "(visual_id, thread_id, kind, title, mime, path, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (visual_id, thread_id, result.kind, result.title, result.mime,
                 str(path), result.source),
            )
        return VisualRecord(
            visual_id=visual_id, thread_id=thread_id, kind=result.kind,
            title=result.title, mime=result.mime, path=str(path),
            source=result.source, created_ts=time.time(),
        )

    async def get(self, visual_id: str) -> VisualRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT visual_id, thread_id, kind, title, mime, path, source, "
                "extract(epoch FROM created_ts) FROM visual_artifacts WHERE visual_id = %s",
                (visual_id,),
            )
            row = await cur.fetchone()
        return _pg_row_to_rec(row) if row else None

    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[VisualRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT visual_id, thread_id, kind, title, mime, path, source, "
                "extract(epoch FROM created_ts) FROM visual_artifacts "
                "WHERE thread_id = %s ORDER BY created_ts DESC LIMIT %s",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
        return [_pg_row_to_rec(r) for r in rows]

    async def delete(self, visual_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE visual_id = %s RETURNING path",
                (visual_id,),
            )
            paths = [r[0] for r in await cur.fetchall()]
        await asyncio.to_thread(_unlink_all, paths)
        return len(paths) > 0

    async def delete_for_thread(self, thread_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE thread_id = %s RETURNING path",
                (thread_id,),
            )
            paths = [r[0] for r in await cur.fetchall()]
        await asyncio.to_thread(_unlink_all, paths)
        return len(paths)

    async def delete_older_than(self, cutoff_ts: float) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM visual_artifacts WHERE created_ts < to_timestamp(%s) "
                "RETURNING path",
                (cutoff_ts,),
            )
            paths = [r[0] for r in await cur.fetchall()]
        await asyncio.to_thread(_unlink_all, paths)
        return len(paths)


def _pg_row_to_rec(r) -> VisualRecord:
    return VisualRecord(
        visual_id=r[0], thread_id=r[1], kind=r[2], title=r[3], mime=r[4],
        path=r[5], source=r[6], created_ts=float(r[7]),
    )


# Process-singleton default. Postgres is primary: the daemon injects a
# PgVisualStore (sharing its pool) via set_default_visual_store() at boot;
# otherwise this lazily builds the SQLite fallback. Mirrors get/set_default_session_store.
_default_store: "VisualStore | None" = None


def set_default_visual_store(store: VisualStore) -> None:
    global _default_store
    _default_store = store


def get_default_visual_store() -> VisualStore:
    global _default_store
    if _default_store is None:
        from yuyutsava.storage.paths import state_db_path

        _default_store = SqliteVisualStore(state_db_path())
    return _default_store


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _unlink_all(paths: list[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _row_to_rec(r) -> VisualRecord:
    return VisualRecord(
        visual_id=r["visual_id"],
        thread_id=r["thread_id"],
        kind=r["kind"],
        title=r["title"],
        mime=r["mime"],
        path=r["path"],
        source=r["source"],
        created_ts=r["created_ts"],
    )
