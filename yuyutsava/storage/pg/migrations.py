"""Forward-only, numbered Postgres migrations under an advisory lock.

The Postgres analogue of :func:`yuyutsava.storage.base.migration_lock`:
``pg_advisory_lock`` serializes concurrent daemon + CLI boots against the
same database, and a ``schema_meta`` table (mirroring the
``BaseSqliteStore._META_TABLE`` convention) anchors the applied version.

All Phase-1+ DDL for the Postgres backend lives here — artifacts,
thread_summaries, memories (v1), tasks (v2). Later phases append
``(N, sql)`` tuples; never edit an applied migration.

The ``memories.embedding`` column is ``vector(768)`` (nomic-embed-text
dimensionality). If you switch to an embedder with a different dimension,
add a migration that recreates the column — pgvector cannot ALTER dims.
"""

from __future__ import annotations

import logging

from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.pg.migrations")

# Arbitrary-but-stable app-wide advisory lock key ("YUYU" in hex-ish).
_ADVISORY_LOCK_KEY = 0x59555955

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_VERSION_KEY = "schema_version"

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            content     TEXT NOT NULL,
            size_chars  INTEGER NOT NULL,
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS artifacts_thread_idx
            ON artifacts (thread_id);
        CREATE INDEX IF NOT EXISTS artifacts_created_idx
            ON artifacts (created_ts);

        CREATE TABLE IF NOT EXISTS thread_summaries (
            thread_id   TEXT NOT NULL,
            version     INTEGER NOT NULL,
            summary     TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            task_id     TEXT,
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (thread_id, version)
        );

        CREATE TABLE IF NOT EXISTS memories (
            memory_id        TEXT PRIMARY KEY,
            kind             TEXT NOT NULL,
            text             TEXT NOT NULL,
            embedding        vector(768),
            source_thread_id TEXT,
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_ts       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS memories_kind_idx
            ON memories (kind);
        CREATE INDEX IF NOT EXISTS memories_embedding_idx
            ON memories USING hnsw (embedding vector_cosine_ops);
        """,
    ),
    (
        2,
        # Phase 2: first-class task tracking (POST /tasks + GET /tasks).
        # Timestamps are epoch-seconds DOUBLE PRECISION, not TIMESTAMPTZ:
        # the registry reads them back and serves them over the API as the
        # same floats the SQLite twin stores, so the two backends stay
        # byte-identical at the wire boundary.
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id        TEXT PRIMARY KEY,
            origin         TEXT NOT NULL,
            instruction    TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN
                           ('queued','running','done','failed','cancelled')),
            thread_id      TEXT,
            complexity     INTEGER,
            created_ts     DOUBLE PRECISION NOT NULL,
            started_ts     DOUBLE PRECISION,
            finished_ts    DOUBLE PRECISION,
            deferred_ms    INTEGER NOT NULL DEFAULT 0,
            result_summary TEXT,
            error          TEXT
        );
        CREATE INDEX IF NOT EXISTS tasks_status_idx
            ON tasks (status, created_ts);
        """,
    ),
]


async def apply(pool: PgPool) -> None:
    """Apply pending migrations. Safe to call from every process at boot."""
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            await conn.execute(_META_TABLE_SQL)
            cur = await conn.execute(
                "SELECT value FROM schema_meta WHERE key = %s", (_VERSION_KEY,)
            )
            row = await cur.fetchone()
            current = int(row[0]) if row else 0

            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                logger.info("pg migrations: applying v%d", version)
                await conn.execute(sql)
                await conn.execute(
                    """
                    INSERT INTO schema_meta (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (_VERSION_KEY, str(version)),
                )
                current = version
            logger.info("pg migrations: at v%d", current)
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,)
            )
