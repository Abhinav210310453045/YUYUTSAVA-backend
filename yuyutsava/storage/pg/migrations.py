"""Forward-only, numbered Postgres migrations under an advisory lock.

The Postgres analogue of :func:`yuyutsava.storage.base.migration_lock`:
``pg_advisory_lock`` serializes concurrent daemon + CLI boots against the
same database, and a ``schema_meta`` table (mirroring the
``BaseSqliteStore._META_TABLE`` convention) anchors the applied version.

All Phase-1+ DDL for the Postgres backend lives here — artifacts,
thread_summaries, memories (v1), tasks (v2), llm_usage + tasks.model (v3),
the ``threads`` relational hub + backfill (v4), the foreign keys wiring
every child table onto it (v5), and the ``sessions`` table moved in from
SQLite (v6). Later phases append ``(N, sql)`` tuples; never edit an applied
migration.

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
    (
        3,
        # Phase 4: model routing + cost tracking. One llm_usage row per
        # model call; tasks gains the chosen-model column so the
        # "complexity-1 tasks that burned 50k tokens" audit join works.
        # Same epoch-seconds DOUBLE PRECISION convention as tasks (v2).
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            TEXT PRIMARY KEY,
            ts            DOUBLE PRECISION NOT NULL,
            thread_id     TEXT NOT NULL DEFAULT '',
            task_id       TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            est_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS llm_usage_ts_idx ON llm_usage (ts);
        CREATE INDEX IF NOT EXISTS llm_usage_task_idx ON llm_usage (task_id);

        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model TEXT;
        """,
    ),
    (
        4,
        # Phase 7: the `threads` relational hub. A canonical parent row unifies
        # the previously FK-less islands (tasks/llm_usage/artifacts/
        # thread_summaries/memories, joined only by a loose thread_id TEXT) and
        # anchors the bridge to Langfuse — langfuse_session_id == thread_id ==
        # Langfuse session_id (see yuyutsava/core/tracing.py). This migration
        # only creates + backfills the hub and normalizes sentinels; v5 wires
        # the foreign keys once every referenced id is guaranteed to exist.
        """
        CREATE TABLE IF NOT EXISTS threads (
            thread_id           TEXT PRIMARY KEY,
            origin              TEXT,
            workspace           TEXT,
            status              TEXT,
            title               TEXT,
            langfuse_session_id TEXT,
            created_ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_ts          TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Backfill from every existing distinct thread_id. langfuse_session_id
        -- is the thread_id itself (identity bridge to Langfuse sessions).
        INSERT INTO threads (thread_id, langfuse_session_id)
        SELECT tid, tid FROM (
            SELECT thread_id        AS tid FROM tasks            WHERE thread_id IS NOT NULL AND thread_id <> ''
            UNION
            SELECT thread_id        AS tid FROM llm_usage        WHERE thread_id IS NOT NULL AND thread_id <> ''
            UNION
            SELECT thread_id        AS tid FROM artifacts        WHERE thread_id IS NOT NULL AND thread_id <> ''
            UNION
            SELECT thread_id        AS tid FROM thread_summaries WHERE thread_id IS NOT NULL AND thread_id <> ''
            UNION
            SELECT source_thread_id AS tid FROM memories         WHERE source_thread_id IS NOT NULL AND source_thread_id <> ''
        ) src
        ON CONFLICT (thread_id) DO NOTHING;

        -- Normalize FK-hostile sentinels: llm_usage used NOT NULL DEFAULT ''
        -- for thread_id/task_id. Make them nullable and convert '' -> NULL so
        -- the nullable FKs validate (NULL skips the check — these are the
        -- non-thread / non-task usage rows, e.g. raw CLI calls).
        ALTER TABLE llm_usage ALTER COLUMN thread_id DROP NOT NULL;
        ALTER TABLE llm_usage ALTER COLUMN thread_id DROP DEFAULT;
        ALTER TABLE llm_usage ALTER COLUMN task_id   DROP NOT NULL;
        ALTER TABLE llm_usage ALTER COLUMN task_id   DROP DEFAULT;
        UPDATE llm_usage SET thread_id = NULL WHERE thread_id = '';
        UPDATE llm_usage SET task_id   = NULL WHERE task_id   = '';

        -- Drop orphan task references (rows whose task_id was never registered)
        -- so the llm_usage.task_id FK in v5 can validate cleanly.
        UPDATE llm_usage SET task_id = NULL
         WHERE task_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.task_id = llm_usage.task_id);

        -- Optional per-call deep-link into the Langfuse trace (nullable).
        ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS langfuse_trace_id TEXT;
        """,
    ),
    (
        5,
        # Phase 7: wire the foreign keys onto the threads hub. ADD CONSTRAINT
        # ... NOT VALID skips the full-table scan at creation; VALIDATE
        # CONSTRAINT then checks existing rows under a lighter lock. New writes
        # are enforced immediately either way — every Pg child store calls
        # ensure_thread (storage/pg/threads.py) before its insert, so the
        # parent row always exists. The DO/EXCEPTION guards keep re-runs safe.
        """
        DO $$ BEGIN
            ALTER TABLE tasks ADD CONSTRAINT tasks_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id) NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE tasks VALIDATE CONSTRAINT tasks_thread_fk;

        DO $$ BEGIN
            ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id) NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE llm_usage VALIDATE CONSTRAINT llm_usage_thread_fk;

        DO $$ BEGIN
            ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_task_fk
                FOREIGN KEY (task_id) REFERENCES tasks (task_id) NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE llm_usage VALIDATE CONSTRAINT llm_usage_task_fk;

        DO $$ BEGIN
            ALTER TABLE artifacts ADD CONSTRAINT artifacts_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE artifacts VALIDATE CONSTRAINT artifacts_thread_fk;

        DO $$ BEGIN
            ALTER TABLE thread_summaries ADD CONSTRAINT thread_summaries_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE thread_summaries VALIDATE CONSTRAINT thread_summaries_thread_fk;

        DO $$ BEGIN
            ALTER TABLE memories ADD CONSTRAINT memories_thread_fk
                FOREIGN KEY (source_thread_id) REFERENCES threads (thread_id)
                ON DELETE SET NULL NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE memories VALIDATE CONSTRAINT memories_thread_fk;
        """,
    ),
    (
        6,
        # Phase 7: move CLI sessions off the standalone sessions.db SQLite file
        # into Postgres so the session index JOINs the rest of the relational
        # model (sessions.thread_id -> threads -> tasks/llm_usage/...). Mirrors
        # SqliteSessionStore._SCHEMA_SQL (storage/sessions/sqlite_impl.py) with
        # epoch-seconds REAL widened to DOUBLE PRECISION and a thread FK.
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id                 TEXT PRIMARY KEY,
            thread_id          TEXT NOT NULL,
            workspace          TEXT NOT NULL,
            status             TEXT NOT NULL,
            created_at         DOUBLE PRECISION NOT NULL,
            updated_at         DOUBLE PRECISION NOT NULL,
            message_count      INTEGER NOT NULL DEFAULT 0,
            memory_files_count INTEGER NOT NULL DEFAULT 0,
            db_row_bytes       INTEGER NOT NULL DEFAULT 0,
            task_preview       TEXT NOT NULL DEFAULT '',
            schema_version     INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS sessions_workspace_updated_idx
            ON sessions (workspace, updated_at DESC);
        CREATE INDEX IF NOT EXISTS sessions_updated_idx
            ON sessions (updated_at DESC);

        DO $$ BEGIN
            ALTER TABLE sessions ADD CONSTRAINT sessions_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id) NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE sessions VALIDATE CONSTRAINT sessions_thread_fk;
        """,
    ),
    (
        7,
        # Full verbatim conversation transcript — one row per message, the
        # durable analogue of Cursor's per-bubble SQLite rows / Claude Code's
        # per-session JSONL. Mirrors SqliteTranscriptStore._SCHEMA_SQL
        # (context/transcript_store.py) with content as JSONB and a thread FK.
        # The recorder dedups on message_id (ON CONFLICT DO NOTHING), so the
        # UNIQUE constraint is load-bearing, not just an index.
        """
        CREATE TABLE IF NOT EXISTS transcript_messages (
            seq        BIGSERIAL PRIMARY KEY,
            message_id TEXT NOT NULL UNIQUE,
            thread_id  TEXT NOT NULL,
            type       TEXT NOT NULL,
            content    JSONB NOT NULL,
            task_id    TEXT,
            created_ts TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS transcript_thread_idx
            ON transcript_messages (thread_id, seq);
        CREATE INDEX IF NOT EXISTS transcript_created_idx
            ON transcript_messages (created_ts);

        DO $$ BEGIN
            ALTER TABLE transcript_messages ADD CONSTRAINT transcript_messages_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE transcript_messages VALIDATE CONSTRAINT transcript_messages_thread_fk;
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
