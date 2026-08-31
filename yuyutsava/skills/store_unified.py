"""One ``skills`` implementation, both backends — with the asymmetry declared.

Phase 2 step 2.5b (ADR-002), playbook order 9. The first domain where the twins
were **not** the same algorithm in two dialects, so it is worth being explicit
about what unifying does and does not mean here.

Shared (was duplicated, now written once):

* ``upsert`` — the same nine columns, the same ON CONFLICT update list;
* ``all_names`` — identical.

**Not shared, deliberately:** ``search``. Postgres runs pgvector cosine
similarity over an embedding column; SQLite counts ``LIKE`` matches on the
description. Those are different retrieval algorithms with different result
orderings, not one query in two dialects. Collapsing them behind a single SQL
string would mean either dropping semantic search on Postgres or pretending
SQLite has it. So retrieval stays two strategies, chosen by a **declared
capability** — and that is the part this module actually fixes.

Before: ``backfill_embeddings`` existed only on the Postgres twin and was
discovered at three call sites with::

    backfill = getattr(store, "backfill_embeddings", None)
    if backfill is not None:
        ...

Duck typing, so a fourth call site that forgot the guard raises
``AttributeError`` — on SQLite only, in production only.
``test/storage/test_twin_conformance.py`` names this pattern as the bad one and
points at ``ArtifactStore.supports_recall`` as the good one.

After: the method **always exists**. On a backend without vectors it is an
honest no-op returning 0, and ``supports_semantic_search`` says so for callers
that want to log or report. Nothing has to probe.

Parity verified on both live backends by
``test/storage/test_skill_store_parity.py``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.keyword import keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.skills.store import SkillStore
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.dialect import Dialect

if TYPE_CHECKING:  # `skills.registry` pulls in the agent stack, which imports
    # back into skills — a cycle that only bites when this module is imported
    # first. SkillMeta is used purely as an annotation, so deferring it is free.
    from yuyutsava.skills.registry import SkillMeta

logger = logging.getLogger("yuyutsava.skills.store_unified")

_SKILLS_TABLE = PgVectorTable(
    table="skills",
    id_col="name",
    text_col="description",
    extra_cols=("scope", "agent", "name"),
    created_col="updated_ts",
)


class SkillSchema(BaseSqliteStore):
    """SQLite DDL owner. Byte-identical to the retired twin's ``_SCHEMA_SQL``."""

    _SCHEMA_VERSION: ClassVar[int] = 1
    _META_TABLE: ClassVar[str] = "skills_meta"
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS skills_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skills (
            name           TEXT PRIMARY KEY,
            scope          TEXT NOT NULL,
            agent          TEXT,
            description    TEXT NOT NULL,
            body           TEXT NOT NULL,
            requires_tools TEXT NOT NULL DEFAULT '[]',
            source_path    TEXT NOT NULL,
            created_ts     REAL NOT NULL,
            updated_ts     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS skills_agent_idx ON skills (agent);
    """


def _agent_filter(agent: str | None, ph: str) -> tuple[str, list]:
    """``agent IS NULL OR agent = ?`` — mirrors ``SkillRegistry.scan(agent)``.

    A skill with ``agent = NULL`` is shared; one with an agent name belongs to
    that agent alone. Scoping is a security-adjacent behaviour (an agent must
    not read another agent's skills), so the same clause is used by both the
    vector and the keyword path rather than being written twice.
    """
    if agent:
        return f"AND (agent IS NULL OR agent = {ph})", [agent]
    return "", []


class UnifiedSkillStore(SkillStore):
    """``skills`` index. Semantic search where the backend supports it."""

    def __init__(
        self,
        dialect: Dialect,
        *,
        embedder: Any | None = None,
        pool: Any | None = None,
        min_score: float = 0.0,
    ) -> None:
        self._d = dialect
        # An embedder is only usable when the backend can store vectors. Pinning
        # that here means the rest of the class never re-checks both conditions,
        # and a caller who passes an embedder to a SQLite store gets keyword
        # search rather than a crash at query time.
        self._embedder = embedder if dialect.supports_vectors else None
        # The pool is passed rather than read off the dialect: `Dialect` is a
        # Protocol with no pool on it, and reaching for `dialect._pool` would
        # make every future dialect implementation carry that private name.
        self._pool = pool
        self._search = (
            PgVectorSearch(pool, _SKILLS_TABLE, min_score=min_score)
            if (self._embedder is not None and pool is not None) else None
        )

    @property
    def supports_semantic_search(self) -> bool:
        """Whether :meth:`search` ranks by meaning rather than word overlap.

        Declared rather than probed — see the module docstring.
        """
        return self._search is not None

    # -- writes -------------------------------------------------------------

    async def upsert(self, meta: "SkillMeta", body: str) -> None:
        d = self._d
        now = time.time()
        embedding: str | None = None
        if self._embedder is not None:
            try:
                embedding = vector_literal(
                    await self._embedder.embed_one(meta.description, mode="document")
                )
            except Exception:
                # Indexing without a vector is degraded, not broken: the row is
                # still found by keyword, and backfill_embeddings repairs it.
                logger.warning(
                    "skills: embedding failed — indexing %r without vector",
                    meta.name, exc_info=True,
                )

        cols = ["name", "scope", "agent", "description", "body"]
        vals = [f"{d.ph(5)}"]
        params: list[Any] = [meta.name, meta.scope, meta.agent, meta.description, body]
        if d.supports_vectors:
            cols.append("embedding")
            vals.append(f"{d.ph()}::vector")
            params.append(embedding)
        cols += ["requires_tools", "source_path", "created_ts", "updated_ts"]
        vals.append(f"{d.json_param()}, {d.ph()}, {d.ts_param()}, {d.ts_param()}")
        params += [json.dumps(list(meta.requires_tools)), str(meta.path), now, now]

        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols
            # created_ts is set once; an update must not reset it.
            if c not in ("name", "created_ts")
        )

        async def _do(conn):
            await conn.execute(
                f"INSERT INTO skills ({', '.join(cols)}) VALUES ({', '.join(vals)}) "
                f"ON CONFLICT (name) DO UPDATE SET {updates}",
                params,
            )

        await d.write(_do)

    # -- reads --------------------------------------------------------------

    async def all_names(self) -> set[str]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute("SELECT name FROM skills")
            rows = await cur.fetchall()
        return {r["name"] for r in rows}

    async def search(self, query: str, k: int = 5, agent: str | None = None) -> list[Hit]:
        """Semantic search where available, keyword matching otherwise.

        Both paths apply the same agent scope filter, so a degraded backend
        returns worse-ranked results — never another agent's skills.
        """
        if self._search is None:
            return await self._keyword_search(query, k, agent)
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning(
                "skills: query embedding failed — keyword fallback", exc_info=True
            )
            return await self._keyword_search(query, k, agent)
        where, params = _agent_filter(agent, self._d.ph())
        # The pool's own connection, NOT d.reading(): PgVectorSearch builds its
        # Hits by positional index (row[0], row[1], ...), and the dialect's read
        # connection uses dict_row so those lookups would raise. The vector path
        # is Postgres-only anyway, so there is nothing to abstract here.
        async with self._pool.connection() as conn:
            return await self._search.vector_search(
                conn, qvec, k, where=where, params=params
            )

    async def _keyword_search(self, query: str, k: int, agent: str | None) -> list[Hit]:
        """Rank by how many query words appear in the description.

        Runs on both backends: it is the SQLite implementation *and* the
        Postgres fallback when embedding fails, so it cannot live on one twin.
        """
        d = self._d
        words = keyword_tokens(query)
        # CASE WHEN, not "(... LIKE ?)" summed directly: SQLite treats a
        # boolean as 0/1 and adds it happily, Postgres has a real boolean type
        # and refuses ("operator does not exist: boolean + boolean"). The twins
        # papered over this with a Postgres-only "::int" cast; CASE WHEN is
        # standard SQL and needs no dialect hook.
        clauses = " + ".join(
            f"(CASE WHEN LOWER(description) LIKE {d.ph()} THEN 1 ELSE 0 END)"
            for _ in words
        )
        like = [f"%{w}%" for w in words]
        params: list[Any] = [*like, *like]
        where, agent_params = _agent_filter(agent, d.ph())
        params += agent_params
        params.append(k)
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT name, scope, agent, description, ({clauses}) AS hits "
                f"FROM skills WHERE ({clauses}) > 0 {where} "
                f"ORDER BY hits DESC, updated_ts DESC LIMIT {d.ph()}",
                params,
            )
            rows = await cur.fetchall()
        return [
            Hit(
                id=r["name"], text=r["description"],
                # 0.0, not a normalised hit count: the score field means cosine
                # similarity, and inventing a number here would let a caller
                # compare keyword and vector results on one scale.
                score=0.0,
                payload={"scope": r["scope"], "agent": r["agent"], "name": r["name"]},
            )
            for r in rows
        ]

    async def backfill_embeddings(
        self, *, batch_size: int = 64, max_rows: int = 2000
    ) -> int:
        """Embed rows indexed without a vector. Returns rows repaired.

        **Always present**, unlike the twin it replaces. On a backend without
        vectors there is nothing to repair, so it returns 0 — which is the true
        answer, and lets the three call sites that used to
        ``getattr(store, "backfill_embeddings", None)`` just call it.
        """
        if self._search is None:
            return 0
        return await self._search.backfill(
            self._embedder, batch_size=batch_size, max_rows=max_rows
        )


def sqlite_skill_store(db_path: Path | None = None) -> UnifiedSkillStore:
    from yuyutsava.storage.dialect import SqliteDialect
    from yuyutsava.storage.paths import state_db_path

    return UnifiedSkillStore(SqliteDialect(SkillSchema(db_path or state_db_path())))


def pg_skill_store(pool, embedder, *, min_score: float = 0.0) -> UnifiedSkillStore:
    from yuyutsava.storage.dialect import PostgresDialect

    return UnifiedSkillStore(
        PostgresDialect(pool), embedder=embedder, pool=pool, min_score=min_score
    )


__all__ = [
    "SkillSchema",
    "UnifiedSkillStore",
    "pg_skill_store",
    "sqlite_skill_store",
]
