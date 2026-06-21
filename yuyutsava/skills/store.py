"""Skill store: pgvector semantic search over SKILL.md, SQLite keyword twin.

The on-disk ``SKILL.md`` files (see :class:`~yuyutsava.skills.registry.SkillRegistry`)
remain the source of truth — portable, git-committable, human-editable. This
store is the *search index*: it embeds a skill's description on write so the
agent can retrieve only the skills relevant to the task at hand instead of
having every skill dumped into the system prompt.

Built on the shared :mod:`yuyutsava.retrieval` engine — the exact same pgvector
cosine/keyword/backfill machinery as memory. Write contract matches memory:
``upsert`` stores a skill even if embedding fails (still keyword-findable; a
backfill re-embeds on recovery), and the disk file is authoritative regardless.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from yuyutsava.memory.embedder import Embedder
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.keyword import keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.skills.registry import SkillMeta
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.skills.store")

# Column map for the shared pgvector engine. scope/agent/name ride in payload.
_SKILLS_TABLE = PgVectorTable(
    table="skills",
    id_col="name",
    text_col="description",
    extra_cols=("scope", "agent", "name"),
    created_col="updated_ts",
)


class SkillStore(ABC):
    """Index + semantic retrieval for skills. Disk stays the source of truth."""

    @abstractmethod
    async def upsert(self, meta: SkillMeta, body: str) -> None:
        """Index (or re-index) one skill by name."""

    @abstractmethod
    async def search(self, query: str, k: int = 5, agent: str | None = None) -> list[Hit]:
        """Top-k skills relevant to ``query``, scoped to ``agent`` (None = all)."""

    @abstractmethod
    async def all_names(self) -> set[str]:
        """Names already indexed (used by the boot-time sync to find new ones)."""


def _agent_filter(agent: str | None) -> tuple[str, list]:
    """``agent IS NULL OR agent = %s`` — mirrors SkillRegistry.scan(agent)."""
    if agent:
        return "AND (agent IS NULL OR agent = %s)", [agent]
    return "", []


class PgSkillStore(SkillStore):
    """pgvector cosine search over the ``skills`` table (migration v8)."""

    def __init__(
        self, pool: PgPool, embedder: Embedder, *, min_score: float = 0.0
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._search = PgVectorSearch(pool, _SKILLS_TABLE, min_score=min_score)

    async def upsert(self, meta: SkillMeta, body: str) -> None:
        embedding: str | None = None
        try:
            embedding = vector_literal(
                await self._embedder.embed_one(meta.description, mode="document")
            )
        except Exception:
            logger.warning(
                "skills: embedding failed — indexing %r without vector", meta.name,
                exc_info=True,
            )
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO skills
                  (name, scope, agent, description, body, embedding,
                   requires_tools, source_path, updated_ts)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s::jsonb, %s, now())
                ON CONFLICT (name) DO UPDATE SET
                  scope          = EXCLUDED.scope,
                  agent          = EXCLUDED.agent,
                  description    = EXCLUDED.description,
                  body           = EXCLUDED.body,
                  embedding      = EXCLUDED.embedding,
                  requires_tools = EXCLUDED.requires_tools,
                  source_path    = EXCLUDED.source_path,
                  updated_ts     = now()
                """,
                (
                    meta.name, meta.scope, meta.agent, meta.description, body,
                    embedding, json.dumps(list(meta.requires_tools)), str(meta.path),
                ),
            )

    async def search(self, query: str, k: int = 5, agent: str | None = None) -> list[Hit]:
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning("skills: query embedding failed — keyword fallback", exc_info=True)
            return await self._keyword_search(query, k, agent)
        where, params = _agent_filter(agent)
        async with self._pool.connection() as conn:
            return await self._search.vector_search(conn, qvec, k, where=where, params=params)

    async def _keyword_search(self, query: str, k: int, agent: str | None) -> list[Hit]:
        where, params = _agent_filter(agent)
        async with self._pool.connection() as conn:
            return await self._search.keyword_search(conn, query, k, where=where, params=params)

    async def all_names(self) -> set[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT name FROM skills")
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def backfill_embeddings(
        self, *, batch_size: int = 64, max_rows: int = 2000
    ) -> int:
        return await self._search.backfill(
            self._embedder, batch_size=batch_size, max_rows=max_rows
        )


class SqliteSkillStore(BaseSqliteStore, SkillStore):
    """Keyword-match twin for the zero-config SQLite backend (no embeddings)."""

    _SCHEMA_VERSION = 1
    _META_TABLE = "skills_meta"
    _SCHEMA_SQL = """
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

    async def upsert(self, meta: SkillMeta, body: str) -> None:
        now = time.time()

        async def _do(conn):
            await conn.execute(
                """
                INSERT INTO skills
                  (name, scope, agent, description, body, requires_tools,
                   source_path, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  scope=excluded.scope, agent=excluded.agent,
                  description=excluded.description, body=excluded.body,
                  requires_tools=excluded.requires_tools,
                  source_path=excluded.source_path, updated_ts=excluded.updated_ts
                """,
                (
                    meta.name, meta.scope, meta.agent, meta.description, body,
                    json.dumps(list(meta.requires_tools)), str(meta.path), now, now,
                ),
            )

        await self._run_write(_do)

    async def search(self, query: str, k: int = 5, agent: str | None = None) -> list[Hit]:
        await self._ensure_schema()
        words = keyword_tokens(query)
        clauses = " + ".join("(LOWER(description) LIKE ?)" for _ in words)
        like_params = [f"%{w}%" for w in words]
        params: list[Any] = [*like_params, *like_params]
        agent_clause = ""
        if agent:
            agent_clause = "AND (agent IS NULL OR agent = ?)"
            params.append(agent)
        params.append(k)
        async with self._conn() as conn:
            cur = await conn.execute(
                f"""
                SELECT name, scope, agent, description, ({clauses}) AS hits
                FROM skills
                WHERE ({clauses}) > 0 {agent_clause}
                ORDER BY hits DESC, updated_ts DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cur.fetchall()
        return [
            Hit(
                id=r["name"], text=r["description"], score=0.0,
                payload={"scope": r["scope"], "agent": r["agent"], "name": r["name"]},
            )
            for r in rows
        ]

    async def all_names(self) -> set[str]:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute("SELECT name FROM skills")
            rows = await cur.fetchall()
        return {r["name"] for r in rows}


class SkillIndexer:
    """Indexes on-disk skills into a :class:`SkillStore` at boot.

    Disk is the source of truth; this catches the store up to it (skills written
    by a previous process, bundled skills, workspace skills committed to git).
    Idempotent and best-effort — a store outage must not break startup.
    """

    @staticmethod
    async def sync(registry, store: SkillStore) -> int:
        try:
            existing = await store.all_names()
        except Exception:
            logger.warning("skills: index sync skipped — store unavailable", exc_info=True)
            return 0
        count = 0
        for meta in registry.scan():
            if meta.name in existing:
                continue
            try:
                await store.upsert(meta, registry.get_body(meta.name))
                count += 1
            except Exception:
                logger.warning("skills: failed to index %r", meta.name, exc_info=True)
        if count:
            logger.info("skills: indexed %d on-disk skill(s) into the store", count)
        return count
