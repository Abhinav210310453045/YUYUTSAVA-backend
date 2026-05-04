"""
SQLite-backed persistent store for event payloads, proposals, decisions, consent rules.

The daemon owns a single SQLite file at ``~/.yuyutsava/state.db``. All writes
go through a single writer task draining a bounded queue — sources never call
SQLite directly. Reads happen on the asyncio thread under the same connection
(SQLite supports concurrent readers; we don't need a pool for MVP).

Tables (created on first connect):

- event_payloads(event_id PK, topic, ts, payload_json, blob_path)
- proposals(proposal_id PK, event_id, topic, summary, proposed, subagent,
            urgency, created_ts, expires_ts, status)
- decisions(decision_id PK, proposal_id, event_id, outcome, action_summary, ts)
- consent_rules(rule_id PK, topic_glob, match_json, decision, created_ts, expires_ts)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.core.config import yuyutsava_home

logger = logging.getLogger("yuyutsava.events.store")


_SCHEMA = """
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
    status       TEXT NOT NULL CHECK (status IN ('pending','approved','skipped','expired','modified'))
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, expires_ts);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id    TEXT PRIMARY KEY,
    proposal_id    TEXT,
    event_id       TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    action_summary TEXT,
    ts             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS consent_rules (
    rule_id     TEXT PRIMARY KEY,
    topic_glob  TEXT NOT NULL,
    match_json  TEXT NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('auto_approve','auto_skip')),
    created_ts  REAL NOT NULL,
    expires_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_consent_rules_topic ON consent_rules(topic_glob);
"""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """Tier-1 consent record shown to the user *before* any orchestrator LLM call."""

    proposal_id: str
    event_id: str
    topic: str
    summary: str
    proposed: str
    subagent: str
    urgency: int
    created_ts: float
    expires_ts: float
    status: str = "pending"

    @classmethod
    def new(
        cls,
        *,
        event_id: str,
        topic: str,
        summary: str,
        proposed: str,
        subagent: str,
        urgency: int,
        expiry_sec: int,
    ) -> Proposal:
        now = time.time()
        return cls(
            proposal_id=str(ULID()),
            event_id=event_id,
            topic=topic,
            summary=summary,
            proposed=proposed,
            subagent=subagent,
            urgency=urgency,
            created_ts=now,
            expires_ts=now + expiry_sec,
            status="pending",
        )


@dataclass(frozen=True)
class ConsentRule:
    rule_id: str
    topic_glob: str
    match_json: str
    decision: str               # "auto_approve" | "auto_skip"
    created_ts: float
    expires_ts: float | None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """Owns the sqlite connection and the writer task.

    Lifecycle: ``await store.start()`` once at daemon boot, ``await store.stop()``
    at shutdown. The store stays usable for reads after stop returns False on
    pending writes.
    """

    def __init__(self, db_path: Path | None = None, *, write_queue_size: int = 1024) -> None:
        self.db_path = db_path or (yuyutsava_home() / "state.db")
        self._conn: sqlite3.Connection | None = None
        self._write_q: asyncio.Queue[tuple[str, tuple[Any, ...]] | None] = asyncio.Queue(
            maxsize=write_queue_size
        )
        self._writer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers non-blocking against the single writer.
        self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._writer_task = asyncio.create_task(self._writer_loop(), name="store-writer")

    async def stop(self) -> None:
        if self._writer_task is not None:
            await self._write_q.put(None)  # sentinel
            try:
                await asyncio.wait_for(self._writer_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Store writer didn't drain in 5s; cancelling")
                self._writer_task.cancel()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _writer_loop(self) -> None:
        assert self._conn is not None
        while True:
            item = await self._write_q.get()
            if item is None:
                return
            sql, args = item
            try:
                self._conn.execute(sql, args)
                self._conn.commit()
            except Exception:
                logger.exception("Store write failed: %s", sql.split()[0:3])

    # --- writes (queued) -----------------------------------------------------

    async def put_event_payload(
        self,
        *,
        event_id: str,
        topic: str,
        ts: float,
        payload: dict[str, Any],
        blob_path: str | None = None,
    ) -> None:
        await self._write_q.put((
            "INSERT OR REPLACE INTO event_payloads(event_id, topic, ts, payload_json, blob_path) "
            "VALUES(?,?,?,?,?)",
            (event_id, topic, ts, json.dumps(payload, default=str), blob_path),
        ))

    async def put_proposal(self, p: Proposal) -> None:
        await self._write_q.put((
            "INSERT INTO proposals(proposal_id, event_id, topic, summary, proposed, subagent, "
            "urgency, created_ts, expires_ts, status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p.proposal_id, p.event_id, p.topic, p.summary, p.proposed, p.subagent,
             p.urgency, p.created_ts, p.expires_ts, p.status),
        ))

    async def put_decision(
        self,
        *,
        proposal_id: str | None,
        event_id: str,
        outcome: str,
        action_summary: str | None = None,
        ts: float | None = None,
    ) -> None:
        await self._write_q.put((
            "INSERT INTO decisions(decision_id, proposal_id, event_id, outcome, action_summary, ts) "
            "VALUES(?,?,?,?,?,?)",
            (str(ULID()), proposal_id, event_id, outcome, action_summary, ts or time.time()),
        ))

    async def put_consent_rule(self, rule: ConsentRule) -> None:
        await self._write_q.put((
            "INSERT INTO consent_rules(rule_id, topic_glob, match_json, decision, created_ts, expires_ts) "
            "VALUES(?,?,?,?,?,?)",
            (rule.rule_id, rule.topic_glob, rule.match_json, rule.decision,
             rule.created_ts, rule.expires_ts),
        ))

    # --- atomic status flip (synchronous; used by web POST handler) ----------

    def try_set_proposal_status(
        self, proposal_id: str, *, from_status: str, to_status: str
    ) -> bool:
        """Atomically flip pending → approved/skipped/modified. Returns True on success."""
        assert self._conn is not None
        cur = self._conn.execute(
            "UPDATE proposals SET status=? WHERE proposal_id=? AND status=?",
            (to_status, proposal_id, from_status),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # --- reads ---------------------------------------------------------------

    def get_event_payload(self, event_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT topic, ts, payload_json, blob_path FROM event_payloads WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "event_id": event_id,
            "topic": row["topic"],
            "ts": row["ts"],
            "payload": json.loads(row["payload_json"]),
            "blob_path": row["blob_path"],
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_consent_rules(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM consent_rules ORDER BY created_ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recall(self, topic_glob: str, since_sec: float, limit: int = 20) -> list[dict[str, Any]]:
        """Recent decisions matching a topic glob. Used by orchestrator's `recall` tool."""
        assert self._conn is not None
        cutoff = time.time() - since_sec
        # Join via event_payloads to get the topic.
        rows = self._conn.execute(
            """
            SELECT d.outcome, d.action_summary, d.ts, ep.topic
              FROM decisions d
              JOIN event_payloads ep ON ep.event_id = d.event_id
             WHERE d.ts >= ?
             ORDER BY d.ts DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        import fnmatch
        out = []
        for r in rows:
            if fnmatch.fnmatchcase(r["topic"], topic_glob):
                out.append(dict(r))
        return out

    def expire_proposals(self, now: float | None = None) -> int:
        """Flip pending proposals past their expires_ts to 'expired'. Returns count."""
        assert self._conn is not None
        cur = self._conn.execute(
            "UPDATE proposals SET status='expired' "
            "WHERE status='pending' AND expires_ts < ?",
            (now or time.time(),),
        )
        self._conn.commit()
        return cur.rowcount
