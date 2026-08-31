"""Shared DDL for the events database (SQLite side).

Kept in its own module so both the SQLite backend
(:mod:`yuyutsava.storage.events.sqlite_backend`) and the facade
(:mod:`yuyutsava.storage.events.store`) can import the schema without a cycle.
The Postgres twin of these tables lives in migration v9
(:mod:`yuyutsava.storage.pg.migrations`); the two must stay wire-identical.
"""

from __future__ import annotations

import logging

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
    -- Matches Postgres (proposals_event_fk). Before schema v5 SQLite had no
    -- foreign keys at all, so the 7-day event_payloads sweep collected
    -- proposals on Postgres and left them forever on SQLite (finding AC).
    -- Enforcement needs PRAGMA foreign_keys=ON, set in SqliteEventsBackend.open.
    event_id     TEXT NOT NULL REFERENCES event_payloads(event_id) ON DELETE CASCADE,
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
    -- Matches Postgres (decisions_proposal_fk). SET NULL, not CASCADE: the
    -- decision is the audit record and must outlive the proposal it resolved.
    proposal_id    TEXT REFERENCES proposals(proposal_id) ON DELETE SET NULL,
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

logger = logging.getLogger("yuyutsava.storage.events.schema")

SCHEMA_VERSION = 5


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

    await _add_foreign_keys_v5(conn)

    await conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


async def _has_fk(conn: aiosqlite.Connection, table: str) -> bool:
    cur = await conn.execute(f"PRAGMA foreign_key_list({table})")
    rows = await cur.fetchall()
    await cur.close()
    return bool(rows)


async def _add_foreign_keys_v5(conn: aiosqlite.Connection) -> None:
    """Give ``proposals`` and ``decisions`` the FKs Postgres already had.

    Schema v5, closing finding AC. SQLite has no ``ALTER TABLE ADD CONSTRAINT``,
    so the only way to add a foreign key to an existing table is to rebuild it:
    create, copy, drop, rename — the procedure in the SQLite docs.

    **Gated on the actual schema, not the version anchor.** A fresh database
    gets the constraints straight from ``SCHEMA_SQL`` yet still reports
    ``current = 0`` (its ``schema_meta`` row is written at the end of this
    function), so a version-only gate would rebuild two tables that were already
    correct on every first boot. ``PRAGMA foreign_key_list`` answers the question
    that actually matters.

    **Rows that would violate the new constraint are resolved, not left to
    fail.** An existing database can hold proposals whose ``event_payloads`` row
    was already swept — that is precisely the divergence being closed. Since
    ``proposals.event_id`` is ``NOT NULL`` they cannot be orphan-nulled, so they
    are dropped, which is exactly what Postgres did to its equivalent rows via
    the cascade. ``decisions.proposal_id`` *is* nullable, so those are nulled
    instead: the decision is an audit record and must survive.

    Each table is gated independently on ``PRAGMA foreign_key_list``. There was
    briefly an outer "both present -> return early" fast path as well; it was
    removed because no negative control could detect its loss — the per-table
    gates already skip everything it skipped, so it was a second mechanism
    guarding nothing, and a gate that cannot be shown to work is not a gate.

    The caller must have foreign-key enforcement OFF (it is off by default, and
    ``SqliteEventsBackend.open`` only turns it on after migrating) — a rebuild
    with enforcement on would trip over its own intermediate states.
    """
    # proposals first: decisions' new FK points at it, so it must be final
    # before decisions is rebuilt.
    if not await _has_fk(conn, "proposals"):
        cur = await conn.execute(
            "SELECT count(*) FROM proposals WHERE event_id NOT IN "
            "(SELECT event_id FROM event_payloads)"
        )
        orphans = (await cur.fetchone())[0]
        await cur.close()
        if orphans:
            logger.warning(
                "events schema v5: dropping %d proposal(s) whose event payload "
                "was already swept — Postgres had removed these via cascade",
                orphans,
            )
        await conn.executescript("""
            CREATE TABLE proposals_v5 (
                proposal_id  TEXT PRIMARY KEY,
                event_id     TEXT NOT NULL REFERENCES event_payloads(event_id) ON DELETE CASCADE,
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
            INSERT INTO proposals_v5
                SELECT proposal_id, event_id, topic, summary, proposed, subagent,
                       urgency, created_ts, expires_ts, status, session_id, agent_path
                  FROM proposals
                 WHERE event_id IN (SELECT event_id FROM event_payloads);
            DROP TABLE proposals;
            ALTER TABLE proposals_v5 RENAME TO proposals;
            CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, expires_ts);
            CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals(session_id);
        """)

    if not await _has_fk(conn, "decisions"):
        await conn.executescript("""
            CREATE TABLE decisions_v5 (
                decision_id    TEXT PRIMARY KEY,
                proposal_id    TEXT REFERENCES proposals(proposal_id) ON DELETE SET NULL,
                event_id       TEXT NOT NULL,
                outcome        TEXT NOT NULL,
                action_summary TEXT,
                ts             REAL NOT NULL,
                session_id     TEXT,
                agent_path     TEXT
            );
            INSERT INTO decisions_v5
                SELECT decision_id,
                       CASE WHEN proposal_id IN (SELECT proposal_id FROM proposals)
                            THEN proposal_id ELSE NULL END,
                       event_id, outcome, action_summary, ts, session_id, agent_path
                  FROM decisions;
            DROP TABLE decisions;
            ALTER TABLE decisions_v5 RENAME TO decisions;
            CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
        """)

    # Rebuilt tables must satisfy their own constraints before we hand the
    # connection back and enforcement is switched on.
    cur = await conn.execute("PRAGMA foreign_key_check")
    violations = await cur.fetchall()
    await cur.close()
    if violations:
        raise RuntimeError(
            f"events schema v5: {len(violations)} foreign-key violation(s) "
            f"survived the rebuild: {violations[:5]}"
        )
