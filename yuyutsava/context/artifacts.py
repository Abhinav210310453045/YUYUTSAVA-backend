"""Artifact store: full bodies of offloaded tool results.

When :class:`~yuyutsava.context.offload_middleware.ToolResultOffloadMiddleware`
intercepts an oversized tool result, the complete content lands here and a
digest referencing the ``artifact_id`` takes its place in graph state. The
agent reads slices back via the always-visible ``ctx_fetch_artifact`` /
``ctx_grep_artifact`` tools.

Two interchangeable backends behind :class:`ArtifactStore`:

- :class:`SqliteArtifactStore` — an ``artifacts`` table in ``state.db``
  (own meta table; coexists with the events store via WAL).
- :class:`PgArtifactStore` — the ``artifacts`` table created by
  :mod:`yuyutsava.storage.pg.migrations`.

Retention: artifacts are scratch, not user data. ``delete_older_than`` is
called by :class:`yuyutsava.storage.sweeper.UnifiedSweeper` on its normal
cadence (default TTL 7 days, ``SweeperConfig.artifact_ttl_sec``).
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.context.artifacts")

# Default slice served by get() — matches the offload threshold so one fetch
# returns at most one "screenful" of context.
DEFAULT_SLICE_CHARS = 20_000
MAX_GREP_MATCHES = 20


def mint_artifact_id() -> str:
    return f"art_{ULID()}"


@dataclass(frozen=True)
class ArtifactSlice:
    """One windowed read of an artifact."""

    artifact_id: str
    content: str
    offset: int
    total_chars: int


class ArtifactStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        """Store ``content``; return the minted ``artifact_id``."""

    @abstractmethod
    async def get(
        self, artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> ArtifactSlice | None:
        """Windowed read. ``None`` when the artifact does not exist."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """TTL sweep hook. Returns rows deleted."""

    async def grep(
        self, artifact_id: str, pattern: str, max_matches: int = MAX_GREP_MATCHES
    ) -> list[str] | None:
        """Regex search over the artifact's lines: ``["<lineno>: <line>", …]``.

        ``None`` when the artifact does not exist; ``[]`` when nothing matched.
        Shared implementation — both backends fetch then match in-process.
        """
        full = await self.get(artifact_id, offset=0, length=-1)
        if full is None:
            return None
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return [f"invalid regex: {exc}"]
        out: list[str] = []
        for i, line in enumerate(full.content.splitlines(), start=1):
            if rx.search(line):
                out.append(f"{i}: {line[:500]}")
                if len(out) >= max_matches:
                    break
        return out


def _slice(content: str, offset: int, length: int) -> tuple[str, int]:
    total = len(content)
    offset = max(0, offset)
    if length < 0:  # internal "whole body" read for grep
        return content[offset:], total
    return content[offset : offset + max(0, length)], total


class SqliteArtifactStore(BaseSqliteStore, ArtifactStore):
    """``artifacts`` table inside ``state.db`` (zero-config fallback)."""

    _SCHEMA_VERSION = 1
    _META_TABLE = "artifacts_meta"
    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS artifacts_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            content     TEXT NOT NULL,
            size_chars  INTEGER NOT NULL,
            created_ts  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS artifacts_thread_idx  ON artifacts (thread_id);
        CREATE INDEX IF NOT EXISTS artifacts_created_idx ON artifacts (created_ts);
    """

    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        artifact_id = mint_artifact_id()

        async def _do(conn):
            await conn.execute(
                "INSERT INTO artifacts "
                "(artifact_id, thread_id, tool_name, content, size_chars, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (artifact_id, thread_id, tool_name, content, len(content), time.time()),
            )

        await self._run_write(_do)
        return artifact_id

    async def get(
        self, artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> ArtifactSlice | None:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT content FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        body, total = _slice(row["content"], offset, length)
        return ArtifactSlice(
            artifact_id=artifact_id, content=body, offset=offset, total_chars=total
        )

    async def delete_older_than(self, cutoff_ts: float) -> int:
        async def _do(conn):
            cur = await conn.execute(
                "DELETE FROM artifacts WHERE created_ts < ?", (cutoff_ts,)
            )
            return cur.rowcount or 0

        return await self._run_write(_do)


class PgArtifactStore(ArtifactStore):
    """``artifacts`` table in Postgres (schema owned by pg/migrations.py)."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        artifact_id = mint_artifact_id()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO artifacts "
                "(artifact_id, thread_id, tool_name, content, size_chars) "
                "VALUES (%s, %s, %s, %s, %s)",
                (artifact_id, thread_id, tool_name, content, len(content)),
            )
        return artifact_id

    async def get(
        self, artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> ArtifactSlice | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT content FROM artifacts WHERE artifact_id = %s",
                (artifact_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        body, total = _slice(row[0], offset, length)
        return ArtifactSlice(
            artifact_id=artifact_id, content=body, offset=offset, total_chars=total
        )

    async def delete_older_than(self, cutoff_ts: float) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM artifacts WHERE created_ts < to_timestamp(%s)",
                (cutoff_ts,),
            )
            return cur.rowcount or 0
