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
        # Timestamps were epoch-seconds DOUBLE PRECISION here; v20 converted
        # them to TIMESTAMPTZ along with the other 16. Kept as written for the
        # historical record — never edit an applied migration.
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
            created_ts     TIMESTAMPTZ      NOT NULL,
            started_ts     TIMESTAMPTZ     ,
            finished_ts    TIMESTAMPTZ     ,
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
        # Same convention as tasks (v2); both converted to TIMESTAMPTZ in v20.
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            TEXT PRIMARY KEY,
            ts            TIMESTAMPTZ      NOT NULL,
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
        # (v20 later converted these to TIMESTAMPTZ.)
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id                 TEXT PRIMARY KEY,
            thread_id          TEXT NOT NULL,
            workspace          TEXT NOT NULL,
            status             TEXT NOT NULL,
            created_at         TIMESTAMPTZ      NOT NULL,
            updated_at         TIMESTAMPTZ      NOT NULL,
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
    (
        8,
        # Skills become semantically retrievable, sharing the memories pgvector
        # machinery (yuyutsava/retrieval). The on-disk SKILL.md files stay the
        # source of truth (portable, git-committable); this table is the search
        # index. PK is the slug `name` (unique across the registry's scope
        # precedence merge) so a re-write is a natural upsert. No thread FK —
        # skills are user/agent-scoped, not thread-scoped. `embedding` is
        # nullable: a skill written while the embedder was down is still
        # keyword-findable and gets backfilled on recovery (same contract as
        # memories). Same vector(768) nomic-embed-text dimensionality.
        """
        CREATE TABLE IF NOT EXISTS skills (
            name           TEXT PRIMARY KEY,
            scope          TEXT NOT NULL,
            agent          TEXT,
            description    TEXT NOT NULL,
            body           TEXT NOT NULL,
            embedding      vector(768),
            requires_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_path    TEXT NOT NULL,
            created_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_ts     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS skills_embedding_idx
            ON skills USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS skills_agent_idx ON skills (agent);
        """,
    ),
    (
        9,
        # Phase 2: give the events DB (events/store.py) and the interrupts audit
        # log (storage/interrupts.py) a Postgres home so the SQLite-only tables
        # can fail over to a SQLite *buffer* and drain back here on recovery
        # (storage/routing). Mirrors the SQLite DDL exactly so the two backends
        # are wire-identical: TEXT-JSON -> JSONB, REAL (epoch seconds) -> DOUBLE
        # PRECISION (NOT timestamptz — values must round-trip unchanged through
        # the buffer). FKs: proposals.event_id -> event_payloads CASCADE;
        # decisions.proposal_id -> proposals SET NULL; decisions.event_id is
        # deliberately loose (the blob sweeper deletes event payloads);
        # interrupts.thread_id -> threads CASCADE (ensure_thread() before insert).
        """
        CREATE TABLE IF NOT EXISTS event_payloads (
            event_id     TEXT PRIMARY KEY,
            topic        TEXT NOT NULL,
            ts           TIMESTAMPTZ      NOT NULL,
            payload_json JSONB NOT NULL,
            blob_path    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_event_payloads_ts ON event_payloads (ts);

        CREATE TABLE IF NOT EXISTS proposals (
            proposal_id  TEXT PRIMARY KEY,
            event_id     TEXT NOT NULL,
            topic        TEXT NOT NULL,
            summary      TEXT NOT NULL,
            proposed     TEXT NOT NULL,
            subagent     TEXT NOT NULL,
            urgency      INTEGER NOT NULL,
            created_ts   TIMESTAMPTZ      NOT NULL,
            expires_ts   TIMESTAMPTZ      NOT NULL,
            status       TEXT NOT NULL CHECK (status IN ('pending','approved','skipped','expired','modified')),
            session_id   TEXT,
            agent_path   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status, expires_ts);
        CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals (session_id);

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id    TEXT PRIMARY KEY,
            proposal_id    TEXT,
            event_id       TEXT NOT NULL,
            outcome        TEXT NOT NULL,
            action_summary TEXT,
            ts             TIMESTAMPTZ      NOT NULL,
            session_id     TEXT,
            agent_path     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);

        CREATE TABLE IF NOT EXISTS consent_rules (
            rule_id     TEXT PRIMARY KEY,
            topic_glob  TEXT NOT NULL,
            match_json  JSONB NOT NULL,
            decision    TEXT NOT NULL CHECK (decision IN ('auto_approve','auto_skip')),
            created_ts  TIMESTAMPTZ      NOT NULL,
            expires_ts  TIMESTAMPTZ     
        );
        CREATE INDEX IF NOT EXISTS idx_consent_rules_topic ON consent_rules (topic_glob);

        CREATE TABLE IF NOT EXISTS tool_call_counters (
            tool_name  TEXT NOT NULL,
            day        TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tool_name, day)
        );

        CREATE TABLE IF NOT EXISTS user_prefs (
            key        TEXT PRIMARY KEY,
            value_json JSONB NOT NULL,
            updated_ts TIMESTAMPTZ      NOT NULL
        );

        CREATE TABLE IF NOT EXISTS consent_grants (
            grant_id    TEXT PRIMARY KEY,
            domain      TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            decision    TEXT NOT NULL,
            scope       TEXT NOT NULL,
            scope_ref   TEXT NOT NULL,
            created_ts  TIMESTAMPTZ      NOT NULL,
            expires_ts  TIMESTAMPTZ     
        );
        CREATE INDEX IF NOT EXISTS idx_consent_grants_domain ON consent_grants (domain, scope_ref);

        CREATE TABLE IF NOT EXISTS interrupts (
            id                TEXT PRIMARY KEY,
            session_id        TEXT NOT NULL,
            thread_id         TEXT NOT NULL,
            agent_path        TEXT NOT NULL,
            requesting_agent  TEXT,
            parent_agent      TEXT,
            invocation_mode   TEXT NOT NULL,
            kind              TEXT NOT NULL,
            operation         TEXT,
            paths_json        JSONB,
            zone              TEXT,
            risk_level        TEXT,
            reason            TEXT,
            question          TEXT,
            payload_json      JSONB NOT NULL,
            outcome           TEXT,
            user_response     TEXT,
            created_at        TIMESTAMPTZ      NOT NULL,
            resolved_at       TIMESTAMPTZ     
        );
        CREATE INDEX IF NOT EXISTS idx_interrupts_session ON interrupts (session_id);
        CREATE INDEX IF NOT EXISTS idx_interrupts_agent_path ON interrupts (agent_path);
        CREATE INDEX IF NOT EXISTS idx_interrupts_created_at ON interrupts (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_interrupts_unresolved ON interrupts (session_id, resolved_at);

        DO $$ BEGIN
            ALTER TABLE proposals ADD CONSTRAINT proposals_event_fk
                FOREIGN KEY (event_id) REFERENCES event_payloads (event_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE proposals VALIDATE CONSTRAINT proposals_event_fk;

        DO $$ BEGIN
            ALTER TABLE decisions ADD CONSTRAINT decisions_proposal_fk
                FOREIGN KEY (proposal_id) REFERENCES proposals (proposal_id)
                ON DELETE SET NULL NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE decisions VALIDATE CONSTRAINT decisions_proposal_fk;

        DO $$ BEGIN
            ALTER TABLE interrupts ADD CONSTRAINT interrupts_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE interrupts VALIDATE CONSTRAINT interrupts_thread_fk;
        """,
    ),
    (
        10,
        # Voice interface: tag each session row with the interface that created
        # it ("cli" | "voice") so the Sessions UI splits voice vs CLI off a DB
        # column. Mirrors SqliteSessionStore v2 (storage/sessions/sqlite_impl.py).
        # The threads table already carries an origin; this denormalizes it onto
        # sessions so the list query needs no JOIN.
        """
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'cli';
        CREATE INDEX IF NOT EXISTS sessions_origin_updated_idx
            ON sessions (origin, updated_at DESC);
        """,
    ),
    (
        11,
        # Phase 6b: voice-conversation surface. One row per spoken turn (role +
        # text + a reference to the synthesized TTS audio) so a resumed voice
        # session can be re-rendered AND replayed. Distinct from
        # transcript_messages (which carries the verbatim LangChain record incl.
        # tool calls) — this is the thin chat-bubble list the Voice UI renders.
        # Mirrors SqliteVoiceMessageStore._SCHEMA_SQL (storage/voice_store.py).
        # Audio bytes live on disk (blobs/voice/); the row holds the path. These
        # are session-scoped user history, dropped on session delete via the
        # thread FK CASCADE — NOT aged out by the blob TTL sweeper.
        """
        CREATE TABLE IF NOT EXISTS voice_messages (
            seq             BIGSERIAL PRIMARY KEY,
            thread_id       TEXT NOT NULL,
            role            TEXT NOT NULL,
            modality        TEXT NOT NULL,
            text            TEXT NOT NULL DEFAULT '',
            audio_blob_path TEXT,
            sample_rate     INTEGER,
            created_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS voice_messages_thread_idx
            ON voice_messages (thread_id, seq);

        DO $$ BEGIN
            ALTER TABLE voice_messages ADD CONSTRAINT voice_messages_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE voice_messages VALIDATE CONSTRAINT voice_messages_thread_fk;
        """,
    ),
    (
        12,
        # Context REPL: semantic index over offloaded tool results. One row per
        # chunk of an artifact's body, embedded so an agent can ctx_recall the
        # relevant slice instead of paging blindly or re-running a search.
        # char_offset maps a chunk back to ctx_fetch_artifact(offset=…) for the
        # full body. Same vector(768) nomic-embed-text dimensionality as
        # memories/skills. Lifecycle is free: ON DELETE CASCADE from artifacts
        # means the existing 7-day artifact TTL sweep also clears these chunks —
        # no new sweeper logic. embedding is nullable (rows written while the
        # embedder is down are re-embedded by PgVectorSearch.backfill).
        """
        CREATE TABLE IF NOT EXISTS artifact_chunks (
            chunk_id    TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            char_offset INTEGER NOT NULL,
            text        TEXT NOT NULL,
            embedding   vector(768),
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS artifact_chunks_embedding_idx
            ON artifact_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS artifact_chunks_thread_idx
            ON artifact_chunks (thread_id);
        CREATE INDEX IF NOT EXISTS artifact_chunks_artifact_idx
            ON artifact_chunks (artifact_id);

        DO $$ BEGIN
            ALTER TABLE artifact_chunks ADD CONSTRAINT artifact_chunks_artifact_fk
                FOREIGN KEY (artifact_id) REFERENCES artifacts (artifact_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE artifact_chunks VALIDATE CONSTRAINT artifact_chunks_artifact_fk;

        DO $$ BEGIN
            ALTER TABLE artifact_chunks ADD CONSTRAINT artifact_chunks_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE artifact_chunks VALIDATE CONSTRAINT artifact_chunks_thread_fk;
        """,
    ),
    (
        13,
        # Per-conversation semantic recall over the verbatim transcript. One row
        # per chunk of a human/assistant turn, embedded so a resumed session can
        # recall what was said even after its LangGraph checkpoint (the agent's
        # working memory) is swept at 1h. ``source_id`` is the message identity
        # (transcript message_id, or "<thread>:<seq>" for voice turns) — indexing
        # skips a source already present, so backfilling old history and live
        # write-through never duplicate. Same vector(768) nomic-embed-text dim as
        # memories/skills/artifact_chunks. Lifecycle is free: ON DELETE CASCADE
        # from threads means deleting a session also clears its chunks. embedding
        # is nullable (rows written while the embedder is down are re-embedded by
        # PgVectorSearch.backfill).
        """
        CREATE TABLE IF NOT EXISTS transcript_chunks (
            chunk_id    TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            role        TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            text        TEXT NOT NULL,
            embedding   vector(768),
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS transcript_chunks_embedding_idx
            ON transcript_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS transcript_chunks_thread_idx
            ON transcript_chunks (thread_id);
        CREATE INDEX IF NOT EXISTS transcript_chunks_source_idx
            ON transcript_chunks (thread_id, source_id);

        DO $$ BEGIN
            ALTER TABLE transcript_chunks ADD CONSTRAINT transcript_chunks_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE transcript_chunks VALIDATE CONSTRAINT transcript_chunks_thread_fk;
        """,
    ),
    (
        14,
        # Rendered-visual metadata index (charts/diagrams/tables/…). The PNG bytes
        # live on disk (blobs/visuals or the workspace _output/visuals); this row
        # holds the path + kind/title so the Artifacts panel and vis_show_artifact
        # can list/serve them. Mirrors SqliteVisualStore._SCHEMA_SQL
        # (yuyutsava/visuals/store.py). Session-scoped output: ON DELETE CASCADE
        # from threads drops the row when a session is purged (purge_session
        # unlinks the files first, before the thread hub is dropped).
        """
        CREATE TABLE IF NOT EXISTS visual_artifacts (
            visual_id   TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            title       TEXT,
            mime        TEXT NOT NULL,
            path        TEXT NOT NULL,
            source      TEXT,
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS visual_artifacts_thread_idx
            ON visual_artifacts (thread_id, created_ts);

        DO $$ BEGIN
            ALTER TABLE visual_artifacts ADD CONSTRAINT visual_artifacts_thread_fk
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
                ON DELETE CASCADE NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        ALTER TABLE visual_artifacts VALIDATE CONSTRAINT visual_artifacts_thread_fk;
        """,
    ),
    (
        15,
        # Message feedback (👍/👎). Mines a future feedback agent for prompt tuning.
        # DELIBERATELY has NO thread FK / CASCADE: feedback is durable insight data
        # that must SURVIVE session deletion (like fact/preference memories). The
        # (user, assistant) text is snapshotted into the row so it stays meaningful
        # after the source transcript is gone. Mirrors SqliteFeedbackStore
        # (yuyutsava/storage/feedback_store.py); re-rating upserts on
        # (thread_id, message_ref).
        """
        CREATE TABLE IF NOT EXISTS message_feedback (
            feedback_id    TEXT PRIMARY KEY,
            thread_id      TEXT NOT NULL,
            session_id     TEXT NOT NULL,
            workspace      TEXT,
            message_ref    TEXT NOT NULL,
            rating         TEXT NOT NULL,
            note           TEXT,
            user_text      TEXT NOT NULL DEFAULT '',
            assistant_text TEXT NOT NULL DEFAULT '',
            created_ts     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS message_feedback_target_idx
            ON message_feedback (thread_id, message_ref);
        CREATE INDEX IF NOT EXISTS message_feedback_recent_idx
            ON message_feedback (created_ts);
        """,
    ),
    (
        16,
        # TODO board (docs/TODO_BOARD_PLAN.md). Cards are the user's GLOBAL
        # planning/thinking surface — durable user data like message_feedback:
        # DELIBERATELY no thread FK and NOT listed in purge_session's tables, so
        # the board survives session deletion. Notes/attachments hang off a card
        # via FK CASCADE (deleting a card is one row delete; the exchange layer
        # unlinks the card's blob dir). workspace_path is the card's tr_*/blob
        # workspace (blobs/todoboard/<card_id>/). pinned is INTEGER 0/1, not
        # BOOLEAN, so the spillover drain (reconcile.py) can replay SQLite twin
        # rows without a type cast. todo_note_chunks is the pgvector recall
        # index over note bodies (same vector(768) nomic-embed-text dim as
        # memories/skills); embedding is nullable for backfill — recall wiring
        # lands in a later phase, the schema ships now. Mirrors
        # SqliteTodoStore._SCHEMA_SQL (yuyutsava/todoboard/store.py).
        """
        CREATE TABLE IF NOT EXISTS todo_cards (
            card_id        TEXT PRIMARY KEY,
            title          TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'inbox'
                           CHECK (status IN ('inbox','active','done','archived')),
            pinned         INTEGER NOT NULL DEFAULT 0,
            tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
            workspace_path TEXT,
            created_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_ts     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_cards_status_idx
            ON todo_cards (status, updated_ts DESC);

        CREATE TABLE IF NOT EXISTS todo_notes (
            note_id     TEXT PRIMARY KEY,
            card_id     TEXT NOT NULL REFERENCES todo_cards (card_id) ON DELETE CASCADE,
            body        TEXT NOT NULL,
            author      TEXT NOT NULL DEFAULT 'user'
                        CHECK (author IN ('user','tinker','master')),
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_notes_card_idx
            ON todo_notes (card_id, created_ts);

        CREATE TABLE IF NOT EXISTS todo_attachments (
            attachment_id TEXT PRIMARY KEY,
            card_id       TEXT NOT NULL REFERENCES todo_cards (card_id) ON DELETE CASCADE,
            kind          TEXT NOT NULL
                          CHECK (kind IN ('file','image','video','link','diagram','artifact')),
            path          TEXT,
            url           TEXT,
            mime          TEXT,
            title         TEXT,
            meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_ts    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_attachments_card_idx
            ON todo_attachments (card_id, created_ts);

        CREATE TABLE IF NOT EXISTS todo_note_chunks (
            chunk_id    TEXT PRIMARY KEY,
            card_id     TEXT NOT NULL,
            note_id     TEXT NOT NULL REFERENCES todo_notes (note_id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL,
            text        TEXT NOT NULL,
            embedding   vector(768),
            created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_note_chunks_embedding_idx
            ON todo_note_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS todo_note_chunks_card_idx
            ON todo_note_chunks (card_id);
        """,
    ),
    (
        17,
        # TODO board think flow. Objectives are a card's structured
        # decomposition — small sub-goals moving thinking → planning → doing →
        # completed (blocked/abandoned as off-ramps); phase IS CHECK'd (closed
        # 6-value flow, like todo_cards.status). Notes gain an optional
        # objective assignment: the FK is ON DELETE SET NULL because a note is
        # the user's thinking and the objective is scaffolding — deleting an
        # objective demotes its notes to card-level "general notes", never
        # destroys them. todo_notes.phase (context the note was written in)
        # has no CHECK: it survives objective deletion as history and is
        # validated at the exchange. todo_events is the card's activity
        # timeline (feeds the "journey" document): objective_id deliberately
        # has NO FK — history must survive objective deletion (same rationale
        # as message_feedback's missing thread FK) — and kind is un-CHECK'd
        # because the event vocabulary grows with the board. No booleans
        # anywhere, honoring the spillover no-cast convention. Mirrors
        # SqliteTodoStore._SCHEMA_SQL v2 (yuyutsava/todoboard/store.py).
        """
        CREATE TABLE IF NOT EXISTS todo_objectives (
            objective_id TEXT PRIMARY KEY,
            card_id      TEXT NOT NULL REFERENCES todo_cards (card_id) ON DELETE CASCADE,
            title        TEXT NOT NULL,
            phase        TEXT NOT NULL DEFAULT 'thinking'
                         CHECK (phase IN ('thinking','planning','doing',
                                          'completed','blocked','abandoned')),
            order_idx    INTEGER NOT NULL DEFAULT 0,
            reason       TEXT,
            outcome      TEXT,
            created_ts   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_ts   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_objectives_card_idx
            ON todo_objectives (card_id, order_idx, created_ts);

        ALTER TABLE todo_notes ADD COLUMN IF NOT EXISTS objective_id TEXT;
        ALTER TABLE todo_notes ADD COLUMN IF NOT EXISTS phase TEXT;
        DO $$ BEGIN
            ALTER TABLE todo_notes ADD CONSTRAINT todo_notes_objective_fk
                FOREIGN KEY (objective_id) REFERENCES todo_objectives (objective_id)
                ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        CREATE INDEX IF NOT EXISTS todo_notes_objective_idx
            ON todo_notes (objective_id);

        CREATE TABLE IF NOT EXISTS todo_events (
            event_id     TEXT PRIMARY KEY,
            card_id      TEXT NOT NULL REFERENCES todo_cards (card_id) ON DELETE CASCADE,
            objective_id TEXT,
            kind         TEXT NOT NULL,
            payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            actor        TEXT NOT NULL DEFAULT 'user',
            created_ts   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS todo_events_card_idx
            ON todo_events (card_id, created_ts);
        """,
    ),
    (
        18,
        # Session titles: set once from the session's first user message so
        # conversation lists (Sessions panel, tinker chat history) can show a
        # human name instead of a raw id. Mirrors SqliteSessionStore v3.
        """
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        19,
        # Durable Tier-2 asks. Nothing here expires: the agent is parked on a
        # LangGraph interrupt and waits indefinitely, so the record must outlive
        # the process that raised it. Written BEFORE broadcast so an ask lost to
        # a dropped SSE frame is still rediscoverable (GET /asks), and a daemon
        # restart can re-enter the owner with Command(resume=…). payload_json is
        # the full wire record (AskPrompt.to_wire_dict) — clients hydrate an
        # identical card from it. Deliberately no FKs: an ask must survive the
        # session or task row it refers to. Mirrors events schema v4
        # (yuyutsava/storage/events/schema.py); the two must stay wire-identical.
        """
        CREATE TABLE IF NOT EXISTS pending_asks (
            ask_id       TEXT PRIMARY KEY,
            created_ts   TIMESTAMPTZ      NOT NULL,
            surface      TEXT NOT NULL,
            session_id   TEXT,
            thread_id    TEXT,
            card_id      TEXT,
            task_id      TEXT,
            interrupt_id TEXT,
            agent_path   TEXT,
            agent_label  TEXT,
            title        TEXT NOT NULL,
            body         TEXT NOT NULL,
            options_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status       TEXT NOT NULL
                         CHECK (status IN ('pending','answered','cancelled')),
            answered_ts  TIMESTAMPTZ     ,
            response     TEXT
        );
        CREATE INDEX IF NOT EXISTS pending_asks_status_idx
            ON pending_asks (status, created_ts);
        CREATE INDEX IF NOT EXISTS pending_asks_thread_idx
            ON pending_asks (thread_id);
        """,
    ),
    (
        20,
        """
        -- One timestamp convention. Before this, 22 columns were TIMESTAMPTZ and
        -- 19 were DOUBLE PRECISION epoch seconds, split along no principle anyone
        -- could state -- which made Dialect.ts_param()/epoch() correct per COLUMN
        -- rather than per backend, and produced finding AK: an insert that failed
        -- with "column is of type double precision but expression is of type
        -- timestamp with time zone".
        --
        -- Each ALTER is guarded on information_schema, so re-running is a no-op
        -- and a database created fresh from the post-v20 CREATE TABLE bodies is
        -- left alone. to_timestamp(NULL) is NULL, so nullable columns convert
        -- with no special case.
        --
        -- SQLite keeps REAL epoch seconds -- it has no timestamp type. That
        -- asymmetry is what the dialect exists to absorb; the point here is that
        -- the rule is now uniform, so ts_param()/epoch() are always correct.
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='consent_grants'
                   AND column_name='created_ts') = 'double precision' THEN
                ALTER TABLE consent_grants ALTER COLUMN created_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(created_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='consent_grants'
                   AND column_name='expires_ts') = 'double precision' THEN
                ALTER TABLE consent_grants ALTER COLUMN expires_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(expires_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='consent_rules'
                   AND column_name='created_ts') = 'double precision' THEN
                ALTER TABLE consent_rules ALTER COLUMN created_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(created_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='consent_rules'
                   AND column_name='expires_ts') = 'double precision' THEN
                ALTER TABLE consent_rules ALTER COLUMN expires_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(expires_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='decisions'
                   AND column_name='ts') = 'double precision' THEN
                ALTER TABLE decisions ALTER COLUMN ts TYPE TIMESTAMPTZ
                    USING to_timestamp(ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='event_payloads'
                   AND column_name='ts') = 'double precision' THEN
                ALTER TABLE event_payloads ALTER COLUMN ts TYPE TIMESTAMPTZ
                    USING to_timestamp(ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='interrupts'
                   AND column_name='created_at') = 'double precision' THEN
                ALTER TABLE interrupts ALTER COLUMN created_at TYPE TIMESTAMPTZ
                    USING to_timestamp(created_at);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='interrupts'
                   AND column_name='resolved_at') = 'double precision' THEN
                ALTER TABLE interrupts ALTER COLUMN resolved_at TYPE TIMESTAMPTZ
                    USING to_timestamp(resolved_at);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='llm_usage'
                   AND column_name='ts') = 'double precision' THEN
                ALTER TABLE llm_usage ALTER COLUMN ts TYPE TIMESTAMPTZ
                    USING to_timestamp(ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='pending_asks'
                   AND column_name='created_ts') = 'double precision' THEN
                ALTER TABLE pending_asks ALTER COLUMN created_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(created_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='pending_asks'
                   AND column_name='answered_ts') = 'double precision' THEN
                ALTER TABLE pending_asks ALTER COLUMN answered_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(answered_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='proposals'
                   AND column_name='created_ts') = 'double precision' THEN
                ALTER TABLE proposals ALTER COLUMN created_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(created_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='proposals'
                   AND column_name='expires_ts') = 'double precision' THEN
                ALTER TABLE proposals ALTER COLUMN expires_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(expires_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='sessions'
                   AND column_name='created_at') = 'double precision' THEN
                ALTER TABLE sessions ALTER COLUMN created_at TYPE TIMESTAMPTZ
                    USING to_timestamp(created_at);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='sessions'
                   AND column_name='updated_at') = 'double precision' THEN
                ALTER TABLE sessions ALTER COLUMN updated_at TYPE TIMESTAMPTZ
                    USING to_timestamp(updated_at);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='tasks'
                   AND column_name='created_ts') = 'double precision' THEN
                ALTER TABLE tasks ALTER COLUMN created_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(created_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='tasks'
                   AND column_name='started_ts') = 'double precision' THEN
                ALTER TABLE tasks ALTER COLUMN started_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(started_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='tasks'
                   AND column_name='finished_ts') = 'double precision' THEN
                ALTER TABLE tasks ALTER COLUMN finished_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(finished_ts);
            END IF;
        END $$;
        DO $$ BEGIN
            IF (SELECT data_type FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='user_prefs'
                   AND column_name='updated_ts') = 'double precision' THEN
                ALTER TABLE user_prefs ALTER COLUMN updated_ts TYPE TIMESTAMPTZ
                    USING to_timestamp(updated_ts);
            END IF;
        END $$;
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
