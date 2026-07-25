"""Shared DDL for the events database (SQLite side).

Kept in its own module so both the SQLite backend
(:mod:`yuyutsava.storage.events.sqlite_backend`) and the facade
(:mod:`yuyutsava.storage.events.store`) can import the schema without a cycle.
The Postgres twin of these tables lives in migration v9
(:mod:`yuyutsava.storage.pg.migrations`); the two must stay wire-identical.
"""

from __future__ import annotations

import aiosqlite

# Mirrors the post-`_migrate` shape exactly (session_id/agent_path inline) so a
# fresh DB needs no ALTERs and an old DB is brought forward by `migrate()`.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS event_payloads (
    event_id     TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    ts           REAL NOT NULL,
    payload_json TEXT NOT NULL,
    blob_path    TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_payloads_ts ON event_payloads(ts);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL,
    topic        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    proposed     TEXT NOT NULL,
    subagent     TEXT NOT NULL,
    urgency      INTEGER NOT NULL,
    created_ts   REAL NOT NULL,
    expires_ts   REAL NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','approved','skipped','expired','modified')),
    session_id   TEXT,
    agent_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, expires_ts);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id    TEXT PRIMARY KEY,
    proposal_id    TEXT,
    event_id       TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    action_summary TEXT,
    ts             REAL NOT NULL,
    session_id     TEXT,
    agent_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consent_rules (
    rule_id     TEXT PRIMARY KEY,
    topic_glob  TEXT NOT NULL,
    match_json  TEXT NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('auto_approve','auto_skip')),
    created_ts  REAL NOT NULL,
    expires_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_consent_rules_topic ON consent_rules(topic_glob);

CREATE TABLE IF NOT EXISTS tool_call_counters (
    tool_name  TEXT NOT NULL,
    day        TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tool_name, day)
);

CREATE TABLE IF NOT EXISTS user_prefs (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS consent_grants (
    grant_id    TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    decision    TEXT NOT NULL,
    scope       TEXT NOT NULL,
    scope_ref   TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    expires_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_consent_grants_domain ON consent_grants(domain, scope_ref);

-- Tier-2 asks awaiting an answer. Nothing here ever expires: the agent is
-- parked on a LangGraph interrupt and waits indefinitely, so the record has to
-- outlive the process that raised it. Written BEFORE the ask is broadcast, so
-- a frame dropped by a slow subscriber (WebHub.broadcast drops on QueueFull)
-- can still be rediscovered via GET /asks, and a daemon restart can re-enter
-- the owner with Command(resume=…). ``payload_json`` is the full wire record
-- (AskPrompt.to_wire_dict) so a hydrating client renders an identical card.
CREATE TABLE IF NOT EXISTS pending_asks (
    ask_id       TEXT PRIMARY KEY,
    created_ts   REAL NOT NULL,
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
    status       TEXT NOT NULL CHECK (status IN ('pending','answered','cancelled')),
    answered_ts  REAL,
    response     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_asks_status ON pending_asks(status, created_ts);
CREATE INDEX IF NOT EXISTS idx_pending_asks_thread ON pending_asks(thread_id);
"""

SCHEMA_VERSION = 4


async def migrate(conn: aiosqlite.Connection) -> None:
    """Forward-only migrations for pre-existing DBs (async port of the original).

    ``CREATE TABLE IF NOT EXISTS`` won't add columns to an existing table, so v0
    DBs need explicit ALTERs to gain ``session_id``/``agent_path``. Gated on the
    ``schema_meta`` version anchor; idempotent.
    """
    cur = await conn.execute("SELECT value FROM schema_meta WHERE key='version'")
    row = await cur.fetchone()
    await cur.close()
    current = int(row[0]) if row else 0

    if current < 2:
        cur = await conn.execute("PRAGMA table_info(proposals)")
        proposal_cols = {r[1] for r in await cur.fetchall()}
        await cur.close()
        if "session_id" not in proposal_cols:
            await conn.execute("ALTER TABLE proposals ADD COLUMN session_id TEXT")
        if "agent_path" not in proposal_cols:
            await conn.execute("ALTER TABLE proposals ADD COLUMN agent_path TEXT")

        cur = await conn.execute("PRAGMA table_info(decisions)")
        decision_cols = {r[1] for r in await cur.fetchall()}
        await cur.close()
        if "session_id" not in decision_cols:
            await conn.execute("ALTER TABLE decisions ADD COLUMN session_id TEXT")
        if "agent_path" not in decision_cols:
            await conn.execute("ALTER TABLE decisions ADD COLUMN agent_path TEXT")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals(session_id)"
        )

    await conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
