"""Postgres twins for the events-domain stores (migration v9 tables).

Mirror the SQLite twins method-for-method. Differences from SQLite:

- ``%s`` placeholders; JSON columns are ``jsonb`` (insert with ``%s::jsonb``).
- psycopg returns ``jsonb`` already parsed to Python objects, so payload/value
  reads need no ``json.loads``. ``consent_rules.match_json`` is re-serialised on
  read because :class:`ConsentRule` keeps it as a JSON *string*.
- timestamps are ``double precision`` (epoch floats), identical to SQLite, so
  values are wire-identical and reconcile is a straight copy.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from typing import Any

from ulid import ULID

from yuyutsava.storage.events.abc import (
    ConsentGrantStore,
    ConsentRuleStore,
    DecisionStore,
    EventStore,
    PrefsBackend,
    ProposalStore,
    ToolCounterStore,
)
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.events.pg")


class PgEventStore(EventStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put_event_payload(
        self, *, event_id: str, topic: str, ts: float,
        payload: dict[str, Any], blob_path: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO event_payloads(event_id, topic, ts, payload_json, blob_path) "
                "VALUES(%s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (event_id) DO UPDATE SET "
                "topic=EXCLUDED.topic, ts=EXCLUDED.ts, "
                "payload_json=EXCLUDED.payload_json, blob_path=EXCLUDED.blob_path",
                (event_id, topic, ts, json.dumps(payload, default=str), blob_path),
            )

    async def get_event_payload(self, event_id: str) -> EventRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT topic, ts, payload_json, blob_path FROM event_payloads WHERE event_id=%s",
                (event_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        payload = row[2]
        if not isinstance(payload, dict):
            payload = {}
        return EventRecord(
            event_id=event_id, topic=row[0], ts=row[1], payload=payload, blob_path=row[3],
        )

    async def delete_event_payloads_with_blob_prefix(self, prefix: str, older_than_ts: float) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM event_payloads WHERE blob_path LIKE %s AND ts < %s",
                (prefix + "%", older_than_ts),
            )
            return cur.rowcount or 0

    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM event_payloads WHERE blob_path IS NULL AND ts < %s",
                (older_than_ts,),
            )
            return cur.rowcount or 0


class PgProposalStore(ProposalStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, p: Proposal) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO proposals(proposal_id, event_id, topic, summary, proposed, subagent, "
                "urgency, created_ts, expires_ts, status, session_id, agent_path) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (proposal_id) DO NOTHING",
                (p.proposal_id, p.event_id, p.topic, p.summary, p.proposed, p.subagent,
                 p.urgency, p.created_ts, p.expires_ts, p.status, p.session_id, p.agent_path),
            )

    async def get(self, proposal_id: str) -> Proposal | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT proposal_id, event_id, topic, summary, proposed, subagent, urgency, "
                "created_ts, expires_ts, status, session_id, agent_path "
                "FROM proposals WHERE proposal_id=%s",
                (proposal_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Proposal(
            proposal_id=row[0], event_id=row[1], topic=row[2], summary=row[3], proposed=row[4],
            subagent=row[5], urgency=row[6], created_ts=row[7], expires_ts=row[8],
            status=row[9], session_id=row[10], agent_path=row[11],
        )

    async def try_set_status(self, proposal_id: str, *, from_status: str, to_status: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE proposals SET status=%s WHERE proposal_id=%s AND status=%s",
                (to_status, proposal_id, from_status),
            )
            return (cur.rowcount or 0) == 1


class PgDecisionStore(DecisionStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(
        self, *, proposal_id: str | None, event_id: str, outcome: str,
        action_summary: str | None = None, ts: float | None = None,
        session_id: str | None = None, agent_path: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO decisions(decision_id, proposal_id, event_id, outcome, action_summary, "
                "ts, session_id, agent_path) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (decision_id) DO NOTHING",
                (str(ULID()), proposal_id, event_id, outcome, action_summary,
                 ts or time.time(), session_id, agent_path),
            )

    async def list(self, limit: int = 50, cursor: float | None = None) -> list[Decision]:
        cols = ("decision_id", "proposal_id", "event_id", "outcome", "action_summary",
                "ts", "session_id", "agent_path")
        async with self._pool.connection() as conn:
            if cursor is not None:
                cur = await conn.execute(
                    f"SELECT {', '.join(cols)} FROM decisions WHERE ts < %s ORDER BY ts DESC LIMIT %s",
                    (float(cursor), limit),
                )
            else:
                cur = await conn.execute(
                    f"SELECT {', '.join(cols)} FROM decisions ORDER BY ts DESC LIMIT %s",
                    (limit,),
                )
            rows = await cur.fetchall()
        return [
            Decision(
                decision_id=r[0], proposal_id=r[1], event_id=r[2], outcome=r[3],
                action_summary=r[4], ts=r[5], session_id=r[6], agent_path=r[7],
            )
            for r in rows
        ]

    async def recall(self, topic_glob: str, since_sec: float, limit: int = 20) -> list[dict[str, Any]]:
        cutoff = time.time() - since_sec
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT d.outcome, d.action_summary, d.ts, ep.topic
                  FROM decisions d
                  JOIN event_payloads ep ON ep.event_id = d.event_id
                 WHERE d.ts >= %s
                 ORDER BY d.ts DESC LIMIT %s
                """,
                (cutoff, limit),
            )
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            if fnmatch.fnmatchcase(r[3], topic_glob):
                out.append({"outcome": r[0], "action_summary": r[1], "ts": r[2], "topic": r[3]})
        return out


class PgConsentRuleStore(ConsentRuleStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, rule: ConsentRule) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO consent_rules(rule_id, topic_glob, match_json, decision, created_ts, expires_ts) "
                "VALUES(%s,%s,%s::jsonb,%s,%s,%s) ON CONFLICT (rule_id) DO NOTHING",
                (rule.rule_id, rule.topic_glob, rule.match_json, rule.decision,
                 rule.created_ts, rule.expires_ts),
            )

    async def list(self) -> list[ConsentRule]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT rule_id, topic_glob, match_json, decision, created_ts, expires_ts "
                "FROM consent_rules ORDER BY created_ts DESC"
            )
            rows = await cur.fetchall()
        out: list[ConsentRule] = []
        for r in rows:
            match = r[2]
            match_json = match if isinstance(match, str) else json.dumps(match)
            out.append(ConsentRule(
                rule_id=r[0], topic_glob=r[1], match_json=match_json,
                decision=r[3], created_ts=r[4], expires_ts=r[5],
            ))
        return out


class PgToolCounterStore(ToolCounterStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def incr(self, tool_name: str, day: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tool_call_counters(tool_name, day, count) VALUES(%s,%s,1) "
                "ON CONFLICT(tool_name, day) DO UPDATE SET count = tool_call_counters.count + 1 "
                "RETURNING count",
                (tool_name, day),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 1

    async def get(self, tool_name: str, day: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count FROM tool_call_counters WHERE tool_name=%s AND day=%s",
                (tool_name, day),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0


class PgPrefsBackend(PrefsBackend):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, key: str, value: Any) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO user_prefs(key, value_json, updated_ts) VALUES(%s,%s::jsonb,%s) "
                "ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json, "
                "updated_ts=EXCLUDED.updated_ts",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    async def delete(self, key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM user_prefs WHERE key=%s", (key,))

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT value_json FROM user_prefs WHERE key=%s", (key,))
            row = await cur.fetchone()
        if row is None:
            return default
        return row[0]

    async def list(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT key, value_json FROM user_prefs ORDER BY key")
            rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


class PgConsentGrantStore(ConsentGrantStore):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, grant: "Grant") -> None:  # noqa: F821
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO consent_grants"
                "(grant_id, domain, subject_key, decision, scope, scope_ref, created_ts, expires_ts) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (grant_id) DO UPDATE SET "
                "domain=EXCLUDED.domain, subject_key=EXCLUDED.subject_key, "
                "decision=EXCLUDED.decision, scope=EXCLUDED.scope, scope_ref=EXCLUDED.scope_ref, "
                "created_ts=EXCLUDED.created_ts, expires_ts=EXCLUDED.expires_ts",
                (grant.grant_id, grant.domain, grant.subject_key, grant.decision,
                 grant.scope, grant.scope_ref, grant.created_ts, grant.expires_ts),
            )

    async def delete(self, grant_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM consent_grants WHERE grant_id=%s", (grant_id,))

    async def load(self) -> list["Grant"]:  # noqa: F821
        from yuyutsava.consent.models import Grant
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT grant_id, domain, subject_key, decision, scope, scope_ref, "
                "created_ts, expires_ts FROM consent_grants"
            )
            rows = await cur.fetchall()
        return [
            Grant(
                grant_id=r[0], domain=r[1], subject_key=r[2], decision=r[3],
                scope=r[4], scope_ref=r[5], created_ts=r[6], expires_ts=r[7],
            )
            for r in rows
        ]
