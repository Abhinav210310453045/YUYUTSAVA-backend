"""SQLite backend + per-domain twins for the events database.

Per the Phase 2 decision, these twins do NOT use ``BaseSqliteStore``'s
lazy-per-call connection model. Instead a single :class:`SqliteEventsBackend`
holds **one** persistent ``aiosqlite`` connection, opened once with the schema
created **eagerly** at ``Store.start()`` (no first-call latency) and closed at
``Store.stop()``. Writes serialise through an ``asyncio.Lock``; reads share the
connection (aiosqlite already serialises operations on its worker thread).

In Postgres mode this same backend backs the SQLite **buffer** that catches
writes while Postgres is down (see :mod:`yuyutsava.storage.routing`).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

import aiosqlite
from ulid import ULID

from yuyutsava.storage.base import amigration_lock
from yuyutsava.storage.events.abc import (
    ConsentGrantStore,
    ConsentRuleStore,
    DecisionStore,
    EventStore,
    PrefsBackend,
    ProposalStore,
    ToolCounterStore,
)
from yuyutsava.storage.events.schema import SCHEMA_SQL, migrate
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.storage.events.sqlite")


class SqliteEventsBackend:
    """Singleton aiosqlite connection shared by every events-domain twin."""

    def __init__(self, db_path: Path | None = None, *, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path or state_db_path()
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        """Connect and create all schema eagerly. Idempotent."""
        if self._conn is not None:
            return
        await asyncio.to_thread(
            self._db_path.parent.mkdir, parents=True, exist_ok=True
        )
        conn = await aiosqlite.connect(str(self._db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        # Cross-process migration lock: daemon + chat can boot simultaneously.
        async with amigration_lock():
            await conn.executescript(SCHEMA_SQL)
            await migrate(conn)
            await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteEventsBackend.open() must be called first")
        return self._conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._write_lock:
            await self._c.execute(sql, params)
            await self._c.commit()

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._write_lock:
            cur = await self._c.execute(sql, params)
            await self._c.commit()
            rc = cur.rowcount
            await cur.close()
            return rc

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cur = await self._c.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)


# ---------------------------------------------------------------------------
# Per-domain SQLite twins
# ---------------------------------------------------------------------------


class SqliteEventStore(EventStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put_event_payload(
        self, *, event_id: str, topic: str, ts: float,
        payload: dict[str, Any], blob_path: str | None = None,
    ) -> None:
        await self._b.execute(
            "INSERT OR REPLACE INTO event_payloads(event_id, topic, ts, payload_json, blob_path) "
            "VALUES(?,?,?,?,?)",
            (event_id, topic, ts, json.dumps(payload, default=str), blob_path),
        )

    async def get_event_payload(self, event_id: str) -> EventRecord | None:
        row = await self._b.fetchone(
            "SELECT topic, ts, payload_json, blob_path FROM event_payloads WHERE event_id=?",
            (event_id,),
        )
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return EventRecord(
            event_id=event_id, topic=row["topic"], ts=row["ts"],
            payload=payload, blob_path=row["blob_path"],
        )

    async def delete_event_payloads_with_blob_prefix(self, prefix: str, older_than_ts: float) -> int:
        return await self._b.execute_rowcount(
            "DELETE FROM event_payloads WHERE blob_path LIKE ? AND ts < ?",
            (prefix + "%", older_than_ts),
        )

    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int:
        return await self._b.execute_rowcount(
            "DELETE FROM event_payloads WHERE blob_path IS NULL AND ts < ?",
            (older_than_ts,),
        )


class SqliteProposalStore(ProposalStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(self, p: Proposal) -> None:
        await self._b.execute(
            "INSERT INTO proposals(proposal_id, event_id, topic, summary, proposed, subagent, "
            "urgency, created_ts, expires_ts, status, session_id, agent_path) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.proposal_id, p.event_id, p.topic, p.summary, p.proposed, p.subagent,
             p.urgency, p.created_ts, p.expires_ts, p.status, p.session_id, p.agent_path),
        )

    async def get(self, proposal_id: str) -> Proposal | None:
        row = await self._b.fetchone(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
        )
        return _row_to_proposal(row) if row else None

    async def try_set_status(self, proposal_id: str, *, from_status: str, to_status: str) -> bool:
        rc = await self._b.execute_rowcount(
            "UPDATE proposals SET status=? WHERE proposal_id=? AND status=?",
            (to_status, proposal_id, from_status),
        )
        return rc == 1


class SqliteDecisionStore(DecisionStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(
        self, *, proposal_id: str | None, event_id: str, outcome: str,
        action_summary: str | None = None, ts: float | None = None,
        session_id: str | None = None, agent_path: str | None = None,
    ) -> None:
        await self._b.execute(
            "INSERT INTO decisions(decision_id, proposal_id, event_id, outcome, action_summary, ts, "
            "session_id, agent_path) VALUES(?,?,?,?,?,?,?,?)",
            (str(ULID()), proposal_id, event_id, outcome, action_summary, ts or time.time(),
             session_id, agent_path),
        )

    async def list(self, limit: int = 50, cursor: float | None = None) -> list[Decision]:
        if cursor is not None:
            rows = await self._b.fetchall(
                "SELECT * FROM decisions WHERE ts < ? ORDER BY ts DESC LIMIT ?",
                (float(cursor), limit),
            )
        else:
            rows = await self._b.fetchall(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
            )
        return [_row_to_decision(r) for r in rows]

    async def recall(self, topic_glob: str, since_sec: float, limit: int = 20) -> list[dict[str, Any]]:
        cutoff = time.time() - since_sec
        rows = await self._b.fetchall(
            """
            SELECT d.outcome, d.action_summary, d.ts, ep.topic
              FROM decisions d
              JOIN event_payloads ep ON ep.event_id = d.event_id
             WHERE d.ts >= ?
             ORDER BY d.ts DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        return [dict(r) for r in rows if fnmatch.fnmatchcase(r["topic"], topic_glob)]


class SqliteConsentRuleStore(ConsentRuleStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(self, rule: ConsentRule) -> None:
        await self._b.execute(
            "INSERT INTO consent_rules(rule_id, topic_glob, match_json, decision, created_ts, expires_ts) "
            "VALUES(?,?,?,?,?,?)",
            (rule.rule_id, rule.topic_glob, rule.match_json, rule.decision,
             rule.created_ts, rule.expires_ts),
        )

    async def list(self) -> list[ConsentRule]:
        rows = await self._b.fetchall("SELECT * FROM consent_rules ORDER BY created_ts DESC")
        return [_row_to_consent_rule(r) for r in rows]


class SqliteToolCounterStore(ToolCounterStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def incr(self, tool_name: str, day: str) -> int:
        async with self._b._write_lock:
            await self._b._c.execute(
                "INSERT INTO tool_call_counters(tool_name, day, count) VALUES(?,?,1) "
                "ON CONFLICT(tool_name, day) DO UPDATE SET count = count + 1",
                (tool_name, day),
            )
            await self._b._c.commit()
            cur = await self._b._c.execute(
                "SELECT count FROM tool_call_counters WHERE tool_name=? AND day=?",
                (tool_name, day),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row["count"]) if row else 1

    async def get(self, tool_name: str, day: str) -> int:
        row = await self._b.fetchone(
            "SELECT count FROM tool_call_counters WHERE tool_name=? AND day=?",
            (tool_name, day),
        )
        return int(row["count"]) if row else 0


class SqlitePrefsBackend(PrefsBackend):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(self, key: str, value: Any) -> None:
        await self._b.execute(
            "INSERT INTO user_prefs(key, value_json, updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_ts=excluded.updated_ts",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    async def delete(self, key: str) -> None:
        await self._b.execute("DELETE FROM user_prefs WHERE key=?", (key,))

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._b.fetchone("SELECT value_json FROM user_prefs WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    async def list(self) -> dict[str, Any]:
        rows = await self._b.fetchall("SELECT key, value_json FROM user_prefs ORDER BY key")
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except Exception:
                pass
        return out


class SqliteConsentGrantStore(ConsentGrantStore):
    def __init__(self, backend: SqliteEventsBackend) -> None:
        self._b = backend

    async def put(self, grant: "Grant") -> None:  # noqa: F821
        await self._b.execute(
            "INSERT OR REPLACE INTO consent_grants"
            "(grant_id, domain, subject_key, decision, scope, scope_ref, created_ts, expires_ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (grant.grant_id, grant.domain, grant.subject_key, grant.decision,
             grant.scope, grant.scope_ref, grant.created_ts, grant.expires_ts),
        )

    async def delete(self, grant_id: str) -> None:
        await self._b.execute("DELETE FROM consent_grants WHERE grant_id=?", (grant_id,))

    async def load(self) -> list["Grant"]:  # noqa: F821
        from yuyutsava.consent.models import Grant
        rows = await self._b.fetchall("SELECT * FROM consent_grants")
        return [
            Grant(
                grant_id=r["grant_id"], domain=r["domain"], subject_key=r["subject_key"],
                decision=r["decision"], scope=r["scope"], scope_ref=r["scope_ref"],
                created_ts=r["created_ts"], expires_ts=r["expires_ts"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


def _row_to_proposal(row: aiosqlite.Row) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"], event_id=row["event_id"], topic=row["topic"],
        summary=row["summary"], proposed=row["proposed"], subagent=row["subagent"],
        urgency=row["urgency"], created_ts=row["created_ts"], expires_ts=row["expires_ts"],
        status=row["status"], session_id=row["session_id"], agent_path=row["agent_path"],
    )


def _row_to_consent_rule(row: aiosqlite.Row) -> ConsentRule:
    return ConsentRule(
        rule_id=row["rule_id"], topic_glob=row["topic_glob"], match_json=row["match_json"],
        decision=row["decision"], created_ts=row["created_ts"], expires_ts=row["expires_ts"],
    )


def _row_to_decision(row: aiosqlite.Row) -> Decision:
    return Decision(
        decision_id=row["decision_id"], proposal_id=row["proposal_id"], event_id=row["event_id"],
        outcome=row["outcome"], action_summary=row["action_summary"], ts=row["ts"],
        session_id=row["session_id"], agent_path=row["agent_path"],
    )
