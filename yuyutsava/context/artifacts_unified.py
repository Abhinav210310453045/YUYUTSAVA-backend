"""One ``artifacts`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 13. Replaces
``SqliteArtifactStore`` and ``PgArtifactStore`` — 194 lines — following the
pattern from skills and memory: shared storage, declared capability for the
pgvector-only part.

Artifacts are the offload target for oversized tool results, so ``put`` sits on
the hot path of every large tool call. Three properties follow from that and are
preserved exactly:

* **``get`` is windowed.** Callers read a slice, never the whole body, except
  for the internal ``length=-1`` read that ``grep`` uses.
* **Indexing is fire-and-forget.** Chunking and embedding run in a background
  task so they cannot add latency to the tool turn, and they never raise into
  the caller.
* **``recall`` is a declared capability.** ``supports_recall`` was already the
  *good* pattern — `test_twin_conformance.py` cites it as the model the
  ``getattr`` probes should have followed — so it is kept verbatim, just moved
  onto a store that works on both backends.

**Fixed: ``created_ts`` came from two clocks.** SQLite passed ``time.time()``;
Postgres omitted the column and let ``DEFAULT now()`` fire — the database
server's clock. Fourth domain in a row with this shape (transcripts AE, feedback
AH, memory AI). Here it decides the TTL sweep: ``delete_older_than`` compares
that column against an application-side cutoff, so skew between the app host and
the database host moves the retention boundary by exactly that amount. Written
explicitly on both now.

Parity verified on both live backends by
``test/storage/test_artifact_store_parity.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from yuyutsava.context.artifacts import (
    DEFAULT_SLICE_CHARS,
    ArtifactSlice,
    ArtifactStore,
    RecallHit,
    _slice,
    mint_artifact_id,
    mint_chunk_id,
)
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect

logger = logging.getLogger("yuyutsava.context.artifacts_unified")

_ARTIFACT_CHUNKS_TABLE = PgVectorTable(
    table="artifact_chunks",
    id_col="chunk_id",
    text_col="text",
    extra_cols=("artifact_id", "char_offset"),
)


class ArtifactSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "artifacts_meta"
    _SCHEMA_SQL: ClassVar[str] = """
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


class UnifiedArtifactStore(ArtifactStore):
    """``artifacts`` — offloaded tool results, with recall where available."""

    def __init__(
        self,
        dialect: Dialect,
        *,
        embedder: Any | None = None,
        pool: Any | None = None,
        semantic_recall: bool = False,
        chunk_chars: int = 1_200,
    ) -> None:
        self._d = dialect
        self._embedder = embedder if dialect.supports_vectors else None
        # Passed rather than read off the dialect: `Dialect` is a Protocol with
        # no pool on it, and PgVectorSearch reads rows positionally so it cannot
        # use the dialect's dict_row read connection.
        self._pool = pool
        self._recall_enabled = bool(
            semantic_recall and self._embedder is not None and pool is not None
        )
        self._chunk_chars = chunk_chars
        self._search = (
            PgVectorSearch(pool, _ARTIFACT_CHUNKS_TABLE) if self._recall_enabled else None
        )
        self._index_tasks: set[asyncio.Task] = set()

    @property
    def supports_recall(self) -> bool:
        """Whether :meth:`recall` does anything.

        Declared, not probed — this property is the pattern
        ``test_twin_conformance.py`` cites as the correct one, and callers
        (``bootstrap.py``) already branch on it.
        """
        return self._recall_enabled

    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        d = self._d
        artifact_id = mint_artifact_id()

        async def _do(conn):
            # Postgres carries artifacts_thread_fk; no-op on SQLite.
            await d.ensure_parent(conn, thread_id)
            await conn.execute(
                f"INSERT INTO artifacts "
                f"(artifact_id, thread_id, tool_name, content, size_chars, created_ts) "
                f"VALUES ({d.ph(5)}, {d.ts_param()})",
                (artifact_id, thread_id, tool_name, content, len(content), time.time()),
            )

        await d.write(_do)

        if self._recall_enabled:
            # Fire-and-forget: indexing must never add latency to the tool turn.
            # The artifact row is committed above before the chunk FK insert.
            task = asyncio.create_task(
                self._index_artifact(artifact_id, thread_id, content)
            )
            self._index_tasks.add(task)
            task.add_done_callback(self._index_tasks.discard)
        return artifact_id

    async def _index_artifact(
        self, artifact_id: str, thread_id: str, content: str
    ) -> None:
        """Chunk + embed + insert into ``artifact_chunks``. Never raises.

        Postgres-only by construction (``_recall_enabled`` requires vectors), so
        it uses the pool directly rather than the dialect.
        """
        from yuyutsava.retrieval.chunking import chunk_text

        try:
            chunks = chunk_text(content, target_chars=self._chunk_chars)
            if not chunks:
                return
            try:
                embedded = await self._embedder.embed(
                    [c.text for c in chunks], mode="document"
                )
                vectors: list[str | None] = [vector_literal(v) for v in embedded]
            except Exception:
                # Embedder down: store rows with NULL vectors, and let backfill
                # re-embed them. Losing the chunk entirely would be worse.
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

        Returns ``[]`` when recall is unavailable rather than raising, so a
        caller that skipped the ``supports_recall`` check degrades instead of
        breaking. Hits resolve via
        ``ctx_fetch_artifact(artifact_id, offset=char_offset)``.
        """
        if self._search is None:
            return []
        where = "AND thread_id = %s"
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning(
                "artifacts: recall query embed failed — keyword fallback", exc_info=True
            )
            async with self._pool.connection() as conn:
                hits = await self._search.keyword_search(
                    conn, query, k, where=where, params=[thread_id]
                )
            return [_to_recall_hit(h) for h in hits]
        async with self._pool.connection() as conn:
            hits = await self._search.vector_search(
                conn, qvec, k, where=where, params=[thread_id]
            )
        return [_to_recall_hit(h) for h in hits]

    async def get(
        self, artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> ArtifactSlice | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT content FROM artifacts WHERE artifact_id = {d.ph()}",
                (artifact_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        # By name: the Postgres twin used row[0], which cannot survive the
        # dialect's dict_row read connection (findings AF/AG/AH).
        body, total = _slice(row["content"], offset, length)
        return ArtifactSlice(
            artifact_id=artifact_id, content=body, offset=offset, total_chars=total
        )

    async def delete_older_than(self, cutoff_ts: float) -> int:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM artifacts WHERE created_ts < {d.ts_param()}",
                (cutoff_ts,),
            )
            return cur.rowcount or 0

        return await d.write(_do)


def _to_recall_hit(hit: Any) -> RecallHit:
    snippet = hit.text if len(hit.text) <= 240 else hit.text[:240] + " …"
    return RecallHit(
        artifact_id=hit.payload["artifact_id"],
        char_offset=int(hit.payload["char_offset"]),
        score=float(hit.score),
        snippet=snippet,
    )


def sqlite_artifact_store(db_path: Path | None = None) -> UnifiedArtifactStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedArtifactStore(
        SqliteDialect(ArtifactSchema(db_path or state_db_path()))
    )


def pg_artifact_store(
    pool, *, embedder: Any | None = None, semantic_recall: bool = False,
    chunk_chars: int = 1_200,
) -> UnifiedArtifactStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedArtifactStore(
        PostgresDialect(pool), embedder=embedder, pool=pool,
        semantic_recall=semantic_recall, chunk_chars=chunk_chars,
    )


__all__ = [
    "ArtifactSchema",
    "UnifiedArtifactStore",
    "pg_artifact_store",
    "sqlite_artifact_store",
]
