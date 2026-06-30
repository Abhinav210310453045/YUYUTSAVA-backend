"""Memory store: pgvector-backed semantic search with a SQLite keyword twin.

The retrieval mechanics (cosine search, keyword fallback, dedup probe, backfill)
live in the reusable :mod:`yuyutsava.retrieval` package and are shared with the
skills store. This module keeps only *memory policy*: valid kinds, the
``RELEVANT MEMORY`` write contract, thread linkage, and the public
``MemoryStore``/``MemoryHit`` surface that the rest of the system depends on.

Write contract: ``add`` never raises out to agent code paths *for embedding
failures* — a memory row without an embedding is still keyword-findable and
strictly better than a lost memory. Database failures do raise; callers on
hot paths (compaction, orchestrator loop) wrap in try/except.

Vectors travel as pgvector's text literal (``'[0.1,0.2,…]'::vector``) so no
per-connection type registration is needed through the pool.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from yuyutsava.memory.embedder import Embedder
from yuyutsava.retrieval.keyword import keyword_tokens as _keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal as _vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.memory.store")

VALID_KINDS = ("task_outcome", "summary", "fact", "preference")

# Column map for the shared pgvector engine. ``kind`` rides along in the payload.
_MEMORIES_TABLE = PgVectorTable(
    table="memories",
    id_col="memory_id",
    text_col="text",
    extra_cols=("kind",),
)


def mint_memory_id() -> str:
    return f"mem_{ULID()}"


@dataclass(frozen=True)
class MemoryHit:
    memory_id: str
    kind: str
    text: str
    score: float  # cosine similarity in [0,1] on pg; 0.0 on the keyword twin


class MemoryStore(ABC):
    @abstractmethod
    async def add(
        self,
        *,
        kind: str,
        text: str,
        source_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store one memory; returns its id."""

    @abstractmethod
    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        """Top-k most relevant memories for ``query``."""


def _kind_filter(kinds: tuple[str, ...] | None) -> tuple[str, list]:
    """Build the ``AND kind = ANY(%s)`` clause + params for the pg engine."""
    if kinds:
        return "AND kind = ANY(%s)", [list(kinds)]
    return "", []


class PgMemoryStore(MemoryStore):
    """pgvector cosine search (schema in storage/pg/migrations.py)."""

    def __init__(
        self,
        pool: PgPool,
        embedder: Embedder,
        *,
        min_score: float = 0.0,
        dedup_threshold: float = 1.1,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        # > 1.0 disables dedup (default when constructed bare, e.g. in tests).
        self._dedup_threshold = dedup_threshold
        self._search = PgVectorSearch(pool, _MEMORIES_TABLE, min_score=min_score)

    async def add(
        self,
        *,
        kind: str,
        text: str,
        source_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind not in VALID_KINDS:
            kind = "fact"
        memory_id = mint_memory_id()
        embedding: str | None = None
        try:
            embedding = _vector_literal(
                await self._embedder.embed_one(text, mode="document")
            )
        except Exception:
            logger.warning(
                "memory: embedding failed — storing %s without vector", memory_id,
                exc_info=True,
            )
        # Atomic dedup-probe + ensure_thread + insert: the three statements
        # commit together (or roll back together) so a crash can't leave the
        # thread row without its memory or vice-versa.
        async with self._pool.transaction() as conn:
            # Near-duplicate suppression: skip writing when a same-kind memory
            # is already near-identical (cosine >= threshold). Keeps repeated
            # compaction summaries / task outcomes from crowding top-k recall.
            if embedding is not None and self._dedup_threshold <= 1.0:
                dup = await self._search.find_duplicate(
                    conn, embedding, self._dedup_threshold,
                    where="AND kind = %s", params=[kind],
                )
                if dup is not None:
                    logger.debug(
                        "memory: near-duplicate of %s (kind=%s) — skipping insert",
                        dup, kind,
                    )
                    return dup
            # source_thread_id FKs to threads (ON DELETE SET NULL); upsert the
            # parent first so the constraint holds. No-op when it's None.
            await ensure_thread(conn, source_thread_id)
            await conn.execute(
                "INSERT INTO memories "
                "(memory_id, kind, text, embedding, source_thread_id, metadata) "
                "VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb)",
                (
                    memory_id, kind, text, embedding,
                    source_thread_id, json.dumps(metadata or {}),
                ),
            )
        return memory_id

    async def backfill_embeddings(
        self, *, batch_size: int = 64, max_rows: int = 2000
    ) -> int:
        """Re-embed rows stored without a vector and return how many were fixed."""
        return await self._search.backfill(
            self._embedder, batch_size=batch_size, max_rows=max_rows
        )

    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        try:
            qvec = _vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning("memory: query embedding failed — keyword fallback", exc_info=True)
            return await self._keyword_search(query, k, kinds)

        where, params = _kind_filter(kinds)
        async with self._pool.connection() as conn:
            hits = await self._search.vector_search(conn, qvec, k, where=where, params=params)
        return [
            MemoryHit(memory_id=h.id, kind=h.payload["kind"], text=h.text, score=h.score)
            for h in hits
        ]

    async def _keyword_search(
        self, query: str, k: int, kinds: tuple[str, ...] | None
    ) -> list[MemoryHit]:
        where, params = _kind_filter(kinds)
        async with self._pool.connection() as conn:
            hits = await self._search.keyword_search(conn, query, k, where=where, params=params)
        return [
            MemoryHit(memory_id=h.id, kind=h.payload["kind"], text=h.text, score=0.0)
            for h in hits
        ]


class SqliteMemoryStore(BaseSqliteStore, MemoryStore):
    """Keyword-match twin for the zero-config SQLite backend.

    No embeddings — ``search`` does per-word LIKE matching ranked by hit
    count then recency. Documented limitation: enable the Postgres backend
    for real semantic recall.
    """

    _SCHEMA_VERSION = 1
    _META_TABLE = "memories_meta"
    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS memories_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            memory_id        TEXT PRIMARY KEY,
            kind             TEXT NOT NULL,
            text             TEXT NOT NULL,
            source_thread_id TEXT,
            metadata         TEXT NOT NULL DEFAULT '{}',
            created_ts       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memories_kind_idx ON memories (kind);
    """

    async def add(
        self,
        *,
        kind: str,
        text: str,
        source_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind not in VALID_KINDS:
            kind = "fact"
        memory_id = mint_memory_id()

        async def _do(conn):
            await conn.execute(
                "INSERT INTO memories "
                "(memory_id, kind, text, source_thread_id, metadata, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    memory_id, kind, text, source_thread_id,
                    json.dumps(metadata or {}), time.time(),
                ),
            )

        await self._run_write(_do)
        return memory_id

    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        await self._ensure_schema()
        words = _keyword_tokens(query)

        clauses = " + ".join("(LOWER(text) LIKE ?)" for _ in words)
        like_params = [f"%{w}%" for w in words]
        # The hit-count expression appears in both SELECT and WHERE (SQLite
        # forbids aliases in WHERE), so the LIKE params bind twice.
        params: list[Any] = [*like_params, *like_params]
        kind_clause = ""
        if kinds:
            kind_clause = f"AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        params.append(k)

        async with self._conn() as conn:
            cur = await conn.execute(
                f"""
                SELECT memory_id, kind, text, ({clauses}) AS hits
                FROM memories
                WHERE ({clauses}) > 0 {kind_clause}
                ORDER BY hits DESC, created_ts DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cur.fetchall()
        return [
            MemoryHit(memory_id=r["memory_id"], kind=r["kind"], text=r["text"], score=0.0)
            for r in rows
        ]
