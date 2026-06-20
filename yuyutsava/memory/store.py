"""Memory store: pgvector-backed semantic search with a SQLite keyword twin.

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
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.memory.store")

VALID_KINDS = ("task_outcome", "summary", "fact", "preference")


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


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


class PgMemoryStore(MemoryStore):
    """pgvector cosine search (schema in storage/pg/migrations.py)."""

    def __init__(self, pool: PgPool, embedder: Embedder) -> None:
        self._pool = pool
        self._embedder = embedder

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
            embedding = _vector_literal(await self._embedder.embed_one(text))
        except Exception:
            logger.warning(
                "memory: embedding failed — storing %s without vector", memory_id,
                exc_info=True,
            )
        async with self._pool.connection() as conn:
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

    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        try:
            qvec = _vector_literal(await self._embedder.embed_one(query))
        except Exception:
            logger.warning("memory: query embedding failed — keyword fallback", exc_info=True)
            return await self._keyword_search(query, k, kinds)

        kind_clause = "AND kind = ANY(%s)" if kinds else ""
        params: list[Any] = [qvec]
        if kinds:
            params.append(list(kinds))
        params.extend([qvec, k])
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT memory_id, kind, text,
                       1 - (embedding <=> %s::vector) AS score
                FROM memories
                WHERE embedding IS NOT NULL {kind_clause}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            )
            rows = await cur.fetchall()
        return [
            MemoryHit(memory_id=r[0], kind=r[1], text=r[2], score=float(r[3]))
            for r in rows
        ]

    async def _keyword_search(
        self, query: str, k: int, kinds: tuple[str, ...] | None
    ) -> list[MemoryHit]:
        kind_clause = "AND kind = ANY(%s)" if kinds else ""
        params: list[Any] = [f"%{query[:200]}%"]
        if kinds:
            params.append(list(kinds))
        params.append(k)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT memory_id, kind, text FROM memories
                WHERE text ILIKE %s {kind_clause}
                ORDER BY created_ts DESC LIMIT %s
                """,
                params,
            )
            rows = await cur.fetchall()
        return [MemoryHit(memory_id=r[0], kind=r[1], text=r[2], score=0.0) for r in rows]


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
        words = [w for w in query.lower().split() if len(w) >= 3][:8]
        if not words:
            words = [query.lower()[:80]]

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
