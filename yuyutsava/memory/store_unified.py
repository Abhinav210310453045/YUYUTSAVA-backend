"""One ``memories`` implementation, both backends.

Phase 2 step 2.5b (ADR-002), playbook order 12. Replaces ``PgMemoryStore`` and
``SqliteMemoryStore`` — 191 lines — following the pattern established for skills
(finding AF): the *storage* is shared, the *retrieval* is not, and the
difference is a **declared capability** rather than something callers probe for.

**This is the migration that removes the last ``getattr`` probes.**
``backfill_embeddings`` lived only on the Postgres twin and was discovered with
``getattr(store, "backfill_embeddings", None)`` at three call sites. Skills
stopped needing that in step 9; ``SqliteMemoryStore`` was the last store without
the method, so with this store declaring it unconditionally the probes are gone
and the call sites just call.

Three divergences resolved:

**``created_ts`` came from two clocks.** SQLite passed ``time.time()``; Postgres
omitted the column and let ``DEFAULT now()`` fire — the database server's clock.
The third domain in a row with this shape (transcripts, feedback, memory). It
bites harder here than it looks: ``created_ts DESC`` is the **tiebreaker** in
keyword ranking, so two memories written moments apart could order differently
depending on backend. Written explicitly on both now.

**The kind filter used two different constructs.** Postgres built
``AND kind = ANY(%s)`` with a list parameter; SQLite built
``AND kind IN (?,?,?)``. Only one form is portable, so ``IN`` with expanded
placeholders is what both use. This filter is not cosmetic — it is how session
purge keeps ``fact``/``preference`` memories and drops the ephemeral kinds.

**Near-duplicate suppression is inherently Postgres-only.** It compares cosine
similarity, which SQLite cannot do. Left as a declared difference: pretending
otherwise would mean a text-equality shortcut on SQLite that behaves unlike the
real thing.

Parity verified on both live backends by
``test/storage/test_memory_store_parity.py``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from yuyutsava.memory.store import (
    VALID_KINDS,
    MemoryHit,
    MemoryStore,
    _keyword_tokens,
    mint_memory_id,
)
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect

logger = logging.getLogger("yuyutsava.memory.store_unified")

_MEMORIES_TABLE = PgVectorTable(
    table="memories",
    id_col="memory_id",
    text_col="text",
    extra_cols=("kind",),
)


class MemorySchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "memories_meta"
    _SCHEMA_SQL: ClassVar[str] = """
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


def _kind_filter(kinds: tuple[str, ...] | None, d: Dialect) -> tuple[str, list]:
    """``AND kind IN (...)`` — the portable form.

    Postgres used ``kind = ANY(%s)`` with a list parameter and SQLite an
    expanded ``IN``; the two are not interchangeable, and this clause decides
    which memories survive a session purge, so it is worth having exactly one.
    """
    if not kinds:
        return "", []
    return f"AND kind IN ({d.ph(len(kinds))})", list(kinds)


class UnifiedMemoryStore(MemoryStore):
    """``memories`` — semantic recall where the backend supports it."""

    def __init__(
        self,
        dialect: Dialect,
        *,
        embedder: Any | None = None,
        pool: Any | None = None,
        min_score: float = 0.0,
        dedup_threshold: float = 1.1,
    ) -> None:
        self._d = dialect
        # An embedder is only usable where vectors can be stored, so a caller
        # who passes one to a SQLite store gets keyword search rather than a
        # crash at query time.
        self._embedder = embedder if dialect.supports_vectors else None
        # Passed rather than read off the dialect: `Dialect` is a Protocol with
        # no pool on it, and PgVectorSearch builds Hits by positional index so
        # it cannot use the dialect's dict_row read connection either.
        self._pool = pool
        # > 1.0 disables dedup (the default when constructed bare, e.g. in tests).
        self._dedup_threshold = dedup_threshold
        self._search = (
            PgVectorSearch(pool, _MEMORIES_TABLE, min_score=min_score)
            if (self._embedder is not None and pool is not None) else None
        )

    @property
    def supports_semantic_search(self) -> bool:
        """Whether recall ranks by meaning rather than word overlap."""
        return self._search is not None

    async def add(
        self,
        *,
        kind: str,
        text: str,
        source_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind not in VALID_KINDS:
            # Coerced, not rejected: a bad kind must never cost the user a memory.
            kind = "fact"
        d = self._d
        memory_id = mint_memory_id()
        now = time.time()

        embedding: str | None = None
        if self._embedder is not None:
            try:
                embedding = vector_literal(
                    await self._embedder.embed_one(text, mode="document")
                )
            except Exception:
                # Storing without a vector is degraded, not broken: the row is
                # still found by keyword and backfill_embeddings repairs it.
                logger.warning(
                    "memory: embedding failed — storing %s without vector",
                    memory_id, exc_info=True,
                )

        cols = ["memory_id", "kind", "text"]
        vals = [f"{d.ph(3)}"]
        params: list[Any] = [memory_id, kind, text]
        if d.supports_vectors:
            cols.append("embedding")
            vals.append(f"{d.ph()}::vector")
            params.append(embedding)
        cols += ["source_thread_id", "metadata", "created_ts"]
        vals.append(f"{d.ph()}, {d.json_param()}, {d.ts_param()}")
        params += [source_thread_id, json.dumps(metadata or {}), now]

        async def _do(conn):
            # Dedup probe, parent upsert and insert commit together, so a crash
            # cannot leave a thread row without its memory or vice versa.
            if self._search is not None and self._dedup_threshold <= 1.0 and embedding:
                dup = await self._search.find_duplicate(
                    conn, embedding, self._dedup_threshold,
                    where=f"AND kind = {d.ph()}", params=[kind],
                )
                if dup is not None:
                    logger.debug(
                        "memory: near-duplicate of %s (kind=%s) — skipping insert",
                        dup, kind,
                    )
                    return dup
            # source_thread_id FKs to threads (ON DELETE SET NULL) on Postgres;
            # no-op on a falsy id and on SQLite entirely.
            await d.ensure_parent(conn, source_thread_id)
            await conn.execute(
                f"INSERT INTO memories ({', '.join(cols)}) VALUES ({', '.join(vals)})",
                params,
            )
            return memory_id

        return await d.write(_do)

    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        """Semantic recall where available, keyword matching otherwise.

        Both paths apply the same ``kinds`` filter, so a degraded backend
        returns worse-ranked results — never a kind the caller excluded.
        """
        if self._search is None:
            return await self._keyword_search(query, k, kinds)
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning(
                "memory: query embedding failed — keyword fallback", exc_info=True
            )
            return await self._keyword_search(query, k, kinds)
        where, params = _kind_filter(kinds, self._d)
        # The pool's own connection, not d.reading(): PgVectorSearch reads rows
        # positionally and the dialect's read connection uses dict_row.
        async with self._pool.connection() as conn:
            hits = await self._search.vector_search(
                conn, qvec, k, where=where, params=params
            )
        return [
            MemoryHit(memory_id=h.id, kind=h.payload["kind"], text=h.text, score=h.score)
            for h in hits
        ]

    async def _keyword_search(
        self, query: str, k: int, kinds: tuple[str, ...] | None
    ) -> list[MemoryHit]:
        """Rank by how many query words appear in the text, then by recency.

        Runs on both backends: it is SQLite's only ranking *and* Postgres's
        fallback when embedding fails, so it cannot live on one twin.
        """
        d = self._d
        words = _keyword_tokens(query)
        # CASE WHEN rather than summing booleans directly: SQLite treats a
        # boolean as 0/1, Postgres refuses to add them (finding AF).
        clauses = " + ".join(
            f"(CASE WHEN LOWER(text) LIKE {d.ph()} THEN 1 ELSE 0 END)" for _ in words
        )
        like = [f"%{w}%" for w in words]
        # The hit-count expression appears in SELECT and WHERE (SQLite forbids
        # aliases in WHERE), so the LIKE params bind twice.
        params: list[Any] = [*like, *like]
        where, kind_params = _kind_filter(kinds, d)
        params += kind_params
        params.append(k)
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT memory_id, kind, text, ({clauses}) AS hits FROM memories "
                f"WHERE ({clauses}) > 0 {where} "
                f"ORDER BY hits DESC, created_ts DESC LIMIT {d.ph()}",
                params,
            )
            rows = await cur.fetchall()
        return [
            MemoryHit(
                memory_id=r["memory_id"], kind=r["kind"], text=r["text"],
                # 0.0, not a normalised hit count: `score` means cosine
                # similarity, and inventing one would let a caller compare
                # keyword and vector results on the same scale.
                score=0.0,
            )
            for r in rows
        ]

    async def backfill_embeddings(
        self, *, batch_size: int = 64, max_rows: int = 2000
    ) -> int:
        """Embed rows stored without a vector. Returns rows repaired.

        **Always present.** On a backend without vectors there is nothing to
        repair, so 0 is the true answer — and that is what lets the three
        ``getattr(store, "backfill_embeddings", None)`` call sites simply call.
        """
        if self._search is None:
            return 0
        return await self._search.backfill(
            self._embedder, batch_size=batch_size, max_rows=max_rows
        )


def sqlite_memory_store(db_path: Path | None = None) -> UnifiedMemoryStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedMemoryStore(SqliteDialect(MemorySchema(db_path or state_db_path())))


def pg_memory_store(
    pool, embedder, *, min_score: float = 0.0, dedup_threshold: float = 1.1
) -> UnifiedMemoryStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedMemoryStore(
        PostgresDialect(pool), embedder=embedder, pool=pool,
        min_score=min_score, dedup_threshold=dedup_threshold,
    )


__all__ = [
    "MemorySchema",
    "UnifiedMemoryStore",
    "pg_memory_store",
    "sqlite_memory_store",
]
