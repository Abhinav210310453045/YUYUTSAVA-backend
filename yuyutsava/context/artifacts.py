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

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from yuyutsava.retrieval.chunking import chunk_text
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.artifacts")

# Default slice served by get() — matches the offload threshold so one fetch
# returns at most one "screenful" of context.
DEFAULT_SLICE_CHARS = 20_000
MAX_GREP_MATCHES = 20

# Column map for the semantic index (migration v12). char_offset lets a recall
# hit map back to ctx_fetch_artifact(offset=…) for the full surrounding body.
_ARTIFACT_CHUNKS_TABLE = PgVectorTable(
    table="artifact_chunks",
    id_col="chunk_id",
    text_col="text",
    extra_cols=("artifact_id", "char_offset"),
)


def thread_id_from_runtime() -> str:
    """Best-effort thread id from the active LangGraph run config."""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "unknown")
    except Exception:
        return "unknown"


def mint_artifact_id() -> str:
    return f"art_{ULID()}"


def mint_chunk_id() -> str:
    return f"ach_{ULID()}"


@dataclass(frozen=True)
class ArtifactSlice:
    """One windowed read of an artifact."""

    artifact_id: str
    content: str
    offset: int
    total_chars: int


@dataclass(frozen=True)
class RecallHit:
    """One semantic hit from the artifact index."""

    artifact_id: str
    char_offset: int
    score: float
    snippet: str


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
    """``artifacts`` table in Postgres (schema owned by pg/migrations.py).

    When constructed with an *embedder* and ``semantic_recall=True`` it also
    indexes each stored body into ``artifact_chunks`` (migration v12) so callers
    can :meth:`recall` the relevant slice. Indexing is best-effort and runs in a
    background task — it never delays or fails a ``put``.
    """

    def __init__(
        self,
        pool: PgPool,
        *,
        embedder: Any | None = None,
        semantic_recall: bool = False,
        chunk_chars: int = 1_200,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._recall_enabled = bool(semantic_recall and embedder is not None)
        self._chunk_chars = chunk_chars
        self._search = PgVectorSearch(pool, _ARTIFACT_CHUNKS_TABLE)
        self._index_tasks: set[asyncio.Task] = set()

    @property
    def supports_recall(self) -> bool:
        return self._recall_enabled

    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        artifact_id = mint_artifact_id()
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # satisfy artifacts_thread_fk
            await conn.execute(
                "INSERT INTO artifacts "
                "(artifact_id, thread_id, tool_name, content, size_chars) "
                "VALUES (%s, %s, %s, %s, %s)",
                (artifact_id, thread_id, tool_name, content, len(content)),
            )
        if self._recall_enabled:
            # Fire-and-forget: indexing must not add latency to the tool turn.
            # The artifact row is committed (autocommit) before the chunk FK insert.
            task = asyncio.create_task(self._index_artifact(artifact_id, thread_id, content))
            self._index_tasks.add(task)
            task.add_done_callback(self._index_tasks.discard)
        return artifact_id

    async def _index_artifact(self, artifact_id: str, thread_id: str, content: str) -> None:
        """Chunk + embed + insert into artifact_chunks. Best-effort; never raises."""
        try:
            chunks = chunk_text(content, target_chars=self._chunk_chars)
            if not chunks:
                return
            vectors: list[str | None]
            try:
                embedded = await self._embedder.embed(
                    [c.text for c in chunks], mode="document"
                )
                vectors = [vector_literal(v) for v in embedded]
            except Exception:
                # Embedder down: store rows with NULL vectors; backfill() re-embeds.
                logger.warning(
                    "artifacts: chunk embedding failed for %s — storing %d NULL rows",
                    artifact_id, len(chunks), exc_info=True,
                )
                vectors = [None] * len(chunks)
            async with self._pool.connection() as conn:
                for chunk, vec in zip(chunks, vectors):
                    await conn.execute(
                        "INSERT INTO artifact_chunks "
                        "(chunk_id, artifact_id, thread_id, seq, char_offset, text, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                        (
                            mint_chunk_id(), artifact_id, thread_id,
                            chunk.seq, chunk.char_offset, chunk.text, vec,
                        ),
                    )
        except Exception:
            logger.warning("artifacts: indexing %s failed", artifact_id, exc_info=True)

    async def recall(self, thread_id: str, query: str, k: int = 5) -> list[RecallHit]:
        """Semantic top-k over this thread's offloaded artifacts.

        Embeds the query and runs cosine search filtered to *thread_id*; falls
        back to keyword search when the embedder is unavailable. Returns hits the
        agent can resolve via ``ctx_fetch_artifact(artifact_id, offset=char_offset)``.
        """
        where = "AND thread_id = %s"
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning("artifacts: recall query embed failed — keyword fallback", exc_info=True)
            async with self._pool.connection() as conn:
                hits = await self._search.keyword_search(conn, query, k, where=where, params=[thread_id])
            return [self._to_recall_hit(h) for h in hits]
        async with self._pool.connection() as conn:
            hits = await self._search.vector_search(conn, qvec, k, where=where, params=[thread_id])
        return [self._to_recall_hit(h) for h in hits]

    @staticmethod
    def _to_recall_hit(hit: Any) -> RecallHit:
        snippet = hit.text if len(hit.text) <= 240 else hit.text[:240] + " …"
        return RecallHit(
            artifact_id=hit.payload["artifact_id"],
            char_offset=int(hit.payload["char_offset"]),
            score=float(hit.score),
            snippet=snippet,
        )

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
