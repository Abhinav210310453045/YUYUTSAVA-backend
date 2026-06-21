"""Reusable pgvector search engine.

One implementation of the cosine search, keyword fallback, near-duplicate
probe, and NULL-embedding backfill that every semantic store needs. Parametrized
over table/column names via :class:`PgVectorTable` so ``memories`` and ``skills``
(and future tables) share exactly this code instead of copy-pasting SQL.

The search/keyword/dedup methods take an open connection so the *caller* owns
the transaction (the routing/failover layer and multi-statement writes need
that control). ``backfill`` is self-contained — it manages its own short-lived
connections because it is a background sweep, not part of a request.

The SQL here is the verbatim behavior previously inlined in ``PgMemoryStore``:
``1 - (embedding <=> v)`` cosine score, HNSW-driven ``ORDER BY embedding <=> v``,
per-word ``ILIKE`` hit-count ranking, and the dedup ``LIMIT 1`` probe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.keyword import keyword_tokens
from yuyutsava.retrieval.vector import vector_literal

logger = logging.getLogger("yuyutsava.retrieval.pg")


@dataclass(frozen=True)
class PgVectorTable:
    """Column map for one pgvector-backed table."""

    table: str
    id_col: str
    text_col: str
    embedding_col: str = "embedding"
    # Extra columns projected into Hit.payload (e.g. ("kind",) for memory,
    # ("scope","agent","name") for skills). Order is preserved.
    extra_cols: tuple[str, ...] = ()
    created_col: str = "created_ts"

    def select_cols(self) -> str:
        return ", ".join((self.id_col, self.text_col, *self.extra_cols))


class PgVectorSearch:
    """Cosine + keyword retrieval over one :class:`PgVectorTable`."""

    def __init__(
        self, pool: Any, table: PgVectorTable, *, min_score: float = 0.0
    ) -> None:
        self._pool = pool
        self._t = table
        self._min_score = min_score

    # ------------------------------------------------------------------
    # Row → Hit
    # ------------------------------------------------------------------

    def _row_to_hit(self, row: tuple, score: float) -> Hit:
        t = self._t
        n_extra = len(t.extra_cols)
        payload = dict(zip(t.extra_cols, row[2 : 2 + n_extra]))
        return Hit(id=row[0], text=row[1], score=score, payload=payload)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def vector_search(
        self, conn, qvec: str, k: int, *, where: str = "", params: list | tuple = ()
    ) -> list[Hit]:
        """Top-k by cosine similarity. ``where`` is extra ``AND …`` filter SQL
        whose ``%s`` params bind in ``params`` (between the two vector binds).

        Weak hits below ``min_score`` are dropped in Python over the already
        HNSW-ranked top-k (not in SQL), so the index still drives ``ORDER BY``
        and returning fewer than ``k`` is intended.
        """
        t = self._t
        cur = await conn.execute(
            f"""
            SELECT {t.select_cols()},
                   1 - ({t.embedding_col} <=> %s::vector) AS score
            FROM {t.table}
            WHERE {t.embedding_col} IS NOT NULL {where}
            ORDER BY {t.embedding_col} <=> %s::vector
            LIMIT %s
            """,
            [qvec, *params, qvec, k],
        )
        rows = await cur.fetchall()
        hits = []
        for r in rows:
            score = float(r[-1])
            if score >= self._min_score:
                hits.append(self._row_to_hit(r, score))
        return hits

    async def keyword_search(
        self, conn, query: str, k: int, *, where: str = "", params: list | tuple = ()
    ) -> list[Hit]:
        """Per-word ILIKE ranked by hit count then recency — the embed-outage
        and SQLite-twin fallback. Score is 0.0 (no semantic distance)."""
        t = self._t
        words = keyword_tokens(query)
        hits_expr = " + ".join(f"(LOWER({t.text_col}) LIKE %s)::int" for _ in words)
        like_params = [f"%{w}%" for w in words]
        # The hit-count expression appears in both SELECT and WHERE, so the LIKE
        # params bind twice (positional %s).
        cur = await conn.execute(
            f"""
            SELECT {t.select_cols()}, ({hits_expr}) AS hits
            FROM {t.table}
            WHERE ({hits_expr}) > 0 {where}
            ORDER BY hits DESC, {t.created_col} DESC
            LIMIT %s
            """,
            [*like_params, *like_params, *params, k],
        )
        rows = await cur.fetchall()
        return [self._row_to_hit(r, 0.0) for r in rows]

    async def find_duplicate(
        self, conn, qvec: str, threshold: float, *, where: str = "", params: list | tuple = ()
    ) -> str | None:
        """Return the id of an existing row at/above ``threshold`` cosine
        similarity to ``qvec``, else None. Runs on the caller's connection."""
        t = self._t
        cur = await conn.execute(
            f"""
            SELECT {t.id_col}, 1 - ({t.embedding_col} <=> %s::vector) AS score
            FROM {t.table}
            WHERE {t.embedding_col} IS NOT NULL {where}
            ORDER BY {t.embedding_col} <=> %s::vector
            LIMIT 1
            """,
            [qvec, *params, qvec],
        )
        row = await cur.fetchone()
        if row and float(row[1]) >= threshold:
            return row[0]
        return None

    async def backfill(
        self, embedder, *, batch_size: int = 64, max_rows: int = 2000
    ) -> int:
        """Re-embed rows stored without a vector; return how many were fixed.

        Rows written while the embedder was unreachable land with
        ``embedding IS NULL`` and are invisible to vector search forever (it
        filters them out). This sweeps them back in once the embedder recovers,
        aborting quietly on the first embed failure (a NULL row is no worse off
        than before, and we must not hammer a still-down endpoint).
        """
        t = self._t
        total = 0
        while total < max_rows:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {t.id_col}, {t.text_col} FROM {t.table} "
                    f"WHERE {t.embedding_col} IS NULL ORDER BY {t.created_col} LIMIT %s",
                    (min(batch_size, max_rows - total),),
                )
                rows = await cur.fetchall()
            if not rows:
                break
            try:
                vectors = await embedder.embed([r[1] for r in rows], mode="document")
            except Exception:
                logger.warning(
                    "retrieval: backfill aborted — embedder unreachable (%s)",
                    t.table, exc_info=True,
                )
                break
            async with self._pool.connection() as conn:
                for (id_val, _text), vec in zip(rows, vectors):
                    await conn.execute(
                        f"UPDATE {t.table} SET {t.embedding_col} = %s::vector "
                        f"WHERE {t.id_col} = %s",
                        (vector_literal(vec), id_val),
                    )
            total += len(rows)
        if total:
            logger.info("retrieval: backfilled %d embedding(s) in %s", total, t.table)
        return total
