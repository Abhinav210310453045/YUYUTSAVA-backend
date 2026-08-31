"""Unified events-domain stores — one implementation per domain, both backends.

Phase 2 step 2.5b, the ``events:*`` batch. These seven domains differ from the
ones migrated before them: they share **one** ``SqliteEventsBackend`` (a single
persistent connection with eager schema) rather than each owning a
``BaseSqliteStore``. :class:`~yuyutsava.storage.dialect.EventsSqliteDialect`
bridges that; everything else follows the established pattern.

**All seven events domains are migrated**: ``EventStore``, ``ProposalStore``,
``DecisionStore``, ``ConsentRuleStore``, ``ConsentGrantStore``,
``ToolCounterStore``, ``PendingAskStore``. The events package no longer has a
single hand-written twin pair.

Two non-dialect differences had to be resolved rather than papered over:

**jsonb.** Postgres stores ``match_json`` as ``jsonb`` and hands it back as a
parsed ``dict``; SQLite stores and returns TEXT. The unified store normalises on
read so callers always see the JSON *string* the ``ConsentRule`` dataclass
declares.

**Write semantics are per-domain, and deliberately not shared.** ``proposals``
uses ``ON CONFLICT DO NOTHING`` (first write wins — a redelivered event must not
resurrect a proposal the user already decided on) while ``consent_grants``
*replaces* (re-granting the same consent with a wider scope must take effect).
Both twins already agreed on this; it is recorded because the two look alike
enough that a helper unifying them would be exactly the wrong DRY.

**Counter increments.** Postgres used one ``INSERT ... ON CONFLICT DO UPDATE
... RETURNING count``; SQLite did the upsert then a separate ``SELECT``. The
single-statement form is used for both — SQLite has supported ``RETURNING``
since 3.35 — which also removes a read that could observe another writer's
increment.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from typing import Any

from ulid import ULID

from yuyutsava.storage.dialect import Dialect
from yuyutsava.storage.events.ask_wire import (
    _ASK_COLS, ask_record_to_params, ask_row_to_record,
)
from yuyutsava.storage.events.abc import (
    ConsentGrantStore, ConsentRuleStore, DecisionStore, EventStore, PendingAskStore,
    ProposalStore, ToolCounterStore,
)
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal

logger = logging.getLogger("yuyutsava.storage.events.unified")


def _json_text(value: Any) -> str:
    """Normalise a JSON column to the string the dataclasses declare.

    Postgres ``jsonb`` deserialises to a ``dict``; SQLite TEXT stays a ``str``.
    Without this the same row yields different Python types per backend, which
    is precisely the kind of divergence the twins kept producing.
    """
    if value is None:
        return "{}"
    return value if isinstance(value, str) else json.dumps(value)


class UnifiedToolCounterStore(ToolCounterStore):
    """``tool_call_counters`` — per-tool, per-day call counts."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def incr(self, tool_name: str, day: str) -> int:
        d = self._d

        async def _do(conn):
            # One statement: the upsert and the read-back cannot be separated by
            # another writer's increment. The SQLite twin did INSERT-then-SELECT
            # and could observe a different value than it wrote.
            cur = await conn.execute(
                f"INSERT INTO tool_call_counters(tool_name, day, count) "
                f"VALUES({d.ph(2)}, 1) "
                f"ON CONFLICT(tool_name, day) DO UPDATE "
                f"SET count = tool_call_counters.count + 1 "
                f"RETURNING count",
                (tool_name, day),
            )
            row = await cur.fetchone()
            return int(row["count"]) if row else 1

        return await d.write(_do)

    async def get(self, tool_name: str, day: str) -> int:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT count FROM tool_call_counters "
                f"WHERE tool_name = {d.ph()} AND day = {d.ph()}",
                (tool_name, day),
            )
            row = await cur.fetchone()
        return int(row["count"]) if row else 0


class UnifiedConsentRuleStore(ConsentRuleStore):
    """``consent_rules`` — auto-approve / auto-skip rules matched by the triage loop."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(self, rule: ConsentRule) -> None:
        d = self._d
        # ``::jsonb`` on Postgres, plain text on SQLite.
        json_ph = "%s::jsonb" if d.name == "postgres" else "?"

        async def _do(conn):
            # DO NOTHING, not DO UPDATE: rule_id is a fresh ULID per rule
            # (triage_loop.py), so a conflict can only mean the same rule put
            # twice — a retry. There is no edit path for consent rules; if one
            # is ever added it needs an explicit DO UPDATE, because DO NOTHING
            # would drop a real edit silently. See finding W.
            await conn.execute(
                f"INSERT INTO consent_rules"
                f"(rule_id, topic_glob, match_json, decision, created_ts, expires_ts) "
                f"VALUES({d.ph(2)}, {json_ph}, {d.ph()}, {d.ts_param()}, {d.ts_param()}) "
                f"ON CONFLICT(rule_id) DO NOTHING",
                (rule.rule_id, rule.topic_glob, rule.match_json, rule.decision,
                 rule.created_ts, rule.expires_ts),
            )

        await d.write(_do)

    async def list(self) -> list[ConsentRule]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT rule_id, topic_glob, match_json, decision, "
                f"{d.epoch('created_ts')}, {d.epoch('expires_ts')} "
                "FROM consent_rules ORDER BY created_ts DESC"
            )
            rows = await cur.fetchall()
        return [
            ConsentRule(
                rule_id=r["rule_id"],
                topic_glob=r["topic_glob"],
                match_json=_json_text(r["match_json"]),
                decision=r["decision"],
                created_ts=r["created_ts"],
                expires_ts=r["expires_ts"],
            )
            for r in rows
        ]



class UnifiedProposalStore(ProposalStore):
    """``proposals`` — Tier-1 actions awaiting a decision."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(self, p: Proposal) -> None:
        d = self._d

        async def _do(conn):
            # DO NOTHING, not DO UPDATE: a redelivered event must not resurrect
            # a proposal the user already approved or skipped. (consent_grants
            # below deliberately does the opposite — see the module docstring.)
            await conn.execute(
                f"INSERT INTO proposals(proposal_id, event_id, topic, summary, proposed, "
                f"subagent, urgency, created_ts, expires_ts, status, session_id, agent_path) "
                f"VALUES({d.ph(7)}, {d.ts_param()}, {d.ts_param()}, {d.ph(3)}) "
                f"ON CONFLICT (proposal_id) DO NOTHING",
                (p.proposal_id, p.event_id, p.topic, p.summary, p.proposed, p.subagent,
                 p.urgency, p.created_ts, p.expires_ts, p.status, p.session_id, p.agent_path),
            )

        await d.write(_do)

    async def get(self, proposal_id: str) -> Proposal | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT proposal_id, event_id, topic, summary, proposed, subagent, urgency, "
                f"{d.epoch('created_ts')}, {d.epoch('expires_ts')}, status, session_id, agent_path "
                f"FROM proposals WHERE proposal_id = {d.ph()}",
                (proposal_id,),
            )
            row = await cur.fetchone()
        return _row_to_proposal(row) if row else None

    async def try_set_status(
        self, proposal_id: str, *, from_status: str, to_status: str
    ) -> bool:
        """Compare-and-set. Returns whether *this* caller made the transition.

        Single-shot approval depends on it: the UI and the CLI can both approve
        the same proposal, and only one may run the action.
        """
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"UPDATE proposals SET status = {d.ph()} "
                f"WHERE proposal_id = {d.ph()} AND status = {d.ph()}",
                (to_status, proposal_id, from_status),
            )
            return (cur.rowcount or 0) == 1

        return await d.write(_do)


class UnifiedDecisionStore(DecisionStore):
    """``decisions`` — the resolved-action audit log."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(
        self, *, proposal_id: str | None, event_id: str, outcome: str,
        action_summary: str | None = None, ts: float | None = None,
        session_id: str | None = None, agent_path: str | None = None,
    ) -> None:
        d = self._d

        async def _do(conn):
            # decision_id is a fresh ULID per call so a collision is not
            # reachable today; the clause keeps the backends from drifting if
            # that ever changes.
            await conn.execute(
                f"INSERT INTO decisions(decision_id, proposal_id, event_id, outcome, "
                f"action_summary, ts, session_id, agent_path) "
                f"VALUES({d.ph(5)}, {d.ts_param()}, {d.ph(2)}) "
                f"ON CONFLICT (decision_id) DO NOTHING",
                (str(ULID()), proposal_id, event_id, outcome, action_summary,
                 ts if ts is not None else time.time(), session_id, agent_path),
            )

        await d.write(_do)

    async def list(self, limit: int = 50, cursor: float | None = None) -> list[Decision]:
        d = self._d
        cols = ("decision_id", "proposal_id", "event_id", "outcome", "action_summary",
                d.epoch("ts"), "session_id", "agent_path")
        async with d.reading() as conn:
            if cursor is not None:
                # Strictly less-than: keyset pagination, so the boundary row is
                # never served on two consecutive pages.
                cur = await conn.execute(
                    f"SELECT {', '.join(cols)} FROM decisions WHERE ts < {d.ts_param()} "
                    f"ORDER BY ts DESC LIMIT {d.ph()}",
                    (float(cursor), limit),
                )
            else:
                cur = await conn.execute(
                    f"SELECT {', '.join(cols)} FROM decisions ORDER BY ts DESC "
                    f"LIMIT {d.ph()}",
                    (limit,),
                )
            rows = await cur.fetchall()
        return [_row_to_decision(r) for r in rows]

    async def recall(
        self, topic_glob: str, since_sec: float, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Recent decisions whose event topic matches *topic_glob*.

        The glob is applied in Python, not SQL: ``fnmatch`` semantics are not
        ``LIKE`` semantics, and the two backends' pattern operators differ
        again on top of that. Filtering after the fetch is the one place where
        doing less in SQL is the portable choice.
        """
        d = self._d
        cutoff = time.time() - since_sec
        async with d.reading() as conn:
            cur = await conn.execute(
                f"""
                SELECT d.outcome, d.action_summary, {d.epoch("d.ts", "ts")}, ep.topic
                  FROM decisions d
                  JOIN event_payloads ep ON ep.event_id = d.event_id
                 WHERE d.ts >= {d.ts_param()}
                 ORDER BY d.ts DESC LIMIT {d.ph()}
                """,
                (cutoff, limit),
            )
            rows = await cur.fetchall()
        return [
            {"outcome": r["outcome"], "action_summary": r["action_summary"],
             "ts": r["ts"], "topic": r["topic"]}
            for r in rows
            if fnmatch.fnmatchcase(r["topic"], topic_glob)
        ]


class UnifiedConsentGrantStore(ConsentGrantStore):
    """``consent_grants`` — the durable allowlist, loaded once at boot."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(self, grant: "Grant") -> None:  # noqa: F821
        d = self._d

        async def _do(conn):
            # REPLACE, unlike proposals: re-granting the same consent with a
            # wider scope must take effect rather than be silently dropped.
            await conn.execute(
                f"INSERT INTO consent_grants(grant_id, domain, subject_key, decision, "
                f"scope, scope_ref, created_ts, expires_ts) "
                f"VALUES({d.ph(6)}, {d.ts_param()}, {d.ts_param()}) "
                f"ON CONFLICT (grant_id) DO UPDATE SET "
                f"domain = EXCLUDED.domain, subject_key = EXCLUDED.subject_key, "
                f"decision = EXCLUDED.decision, scope = EXCLUDED.scope, "
                f"scope_ref = EXCLUDED.scope_ref, created_ts = EXCLUDED.created_ts, "
                f"expires_ts = EXCLUDED.expires_ts",
                (grant.grant_id, grant.domain, grant.subject_key, grant.decision,
                 grant.scope, grant.scope_ref, grant.created_ts, grant.expires_ts),
            )

        await d.write(_do)

    async def delete(self, grant_id: str) -> None:
        d = self._d

        async def _do(conn):
            await conn.execute(
                f"DELETE FROM consent_grants WHERE grant_id = {d.ph()}", (grant_id,)
            )

        await d.write(_do)

    async def load(self) -> list["Grant"]:  # noqa: F821
        from yuyutsava.consent.models import Grant

        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT grant_id, domain, subject_key, decision, scope, scope_ref, "
                f"{d.epoch('created_ts')}, {d.epoch('expires_ts')} FROM consent_grants"
            )
            rows = await cur.fetchall()
        return [
            Grant(
                grant_id=r["grant_id"], domain=r["domain"], subject_key=r["subject_key"],
                decision=r["decision"], scope=r["scope"], scope_ref=r["scope_ref"],
                created_ts=r["created_ts"], expires_ts=r["expires_ts"],
            )
            for r in rows
        ]


def _row_to_proposal(row: Any) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"], event_id=row["event_id"], topic=row["topic"],
        summary=row["summary"], proposed=row["proposed"], subagent=row["subagent"],
        # SQLite hands INTEGER back as int; keep the declared type explicit so a
        # column type change on either side cannot silently widen it to float.
        urgency=int(row["urgency"]), created_ts=row["created_ts"],
        expires_ts=row["expires_ts"], status=row["status"],
        session_id=row["session_id"], agent_path=row["agent_path"],
    )


def _row_to_decision(row: Any) -> Decision:
    return Decision(
        decision_id=row["decision_id"], proposal_id=row["proposal_id"],
        event_id=row["event_id"], outcome=row["outcome"],
        action_summary=row["action_summary"], ts=row["ts"],
        session_id=row["session_id"], agent_path=row["agent_path"],
    )



class UnifiedEventStore(EventStore):
    """``event_payloads`` — the raw event bodies everything else points at."""

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put_event_payload(
        self, *, event_id: str, topic: str, ts: float,
        payload: dict[str, Any], blob_path: str | None = None,
    ) -> None:
        d = self._d

        async def _do(conn):
            # An upsert, unlike proposals: a re-delivered event carries the
            # current body and should replace the stale one.
            await conn.execute(
                f"INSERT INTO event_payloads(event_id, topic, ts, payload_json, blob_path) "
                f"VALUES({d.ph(2)}, {d.ts_param()}, {d.json_param()}, {d.ph()}) "
                f"ON CONFLICT (event_id) DO UPDATE SET "
                f"topic = EXCLUDED.topic, ts = EXCLUDED.ts, "
                f"payload_json = EXCLUDED.payload_json, blob_path = EXCLUDED.blob_path",
                # default=str because event payloads carry arbitrary objects
                # (Paths, datetimes) straight from the sources.
                (event_id, topic, ts, json.dumps(payload, default=str), blob_path),
            )

        await d.write(_do)

    async def get_event_payload(self, event_id: str) -> EventRecord | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT topic, {d.epoch('ts')}, payload_json, blob_path FROM event_payloads "
                f"WHERE event_id = {d.ph()}",
                (event_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        # jsonb returns a dict, TEXT returns a str: json_value collapses both to
        # the dict every caller indexes into. A malformed body degrades to {}
        # rather than raising — an unreadable payload must not break the read.
        payload = d.json_value(row["payload_json"])
        if not isinstance(payload, dict):
            payload = {}
        return EventRecord(
            event_id=event_id, topic=row["topic"], ts=row["ts"],
            payload=payload, blob_path=row["blob_path"],
        )

    async def delete_event_payloads_with_blob_prefix(
        self, prefix: str, older_than_ts: float
    ) -> int:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM event_payloads WHERE blob_path LIKE {d.ph()} "
                f"AND ts < {d.ts_param()}",
                (prefix + "%", older_than_ts),
            )
            return cur.rowcount or 0

        return await d.write(_do)

    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int:
        d = self._d

        async def _do(conn):
            # blob_path IS NULL is load-bearing: blob-backed rows belong to the
            # blob sweep, which ties row removal to unlinking the file. Deleting
            # them here would orphan the file with nothing left pointing at it.
            cur = await conn.execute(
                f"DELETE FROM event_payloads WHERE blob_path IS NULL AND ts < {d.ts_param()}",
                (older_than_ts,),
            )
            return cur.rowcount or 0

        return await d.write(_do)


class UnifiedPendingAskStore(PendingAskStore):
    """``pending_asks`` — Tier-2 asks the agent is parked on.

    Durable: the agent waits on a LangGraph interrupt indefinitely, so the row
    must outlive the process that raised it. ``put`` runs *before* the ask is
    broadcast, which is what makes a dropped frame recoverable.
    """

    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect

    async def put(self, record: dict[str, Any]) -> None:
        d = self._d
        cols = ", ".join(_ASK_COLS)

        async def _do(conn):
            # DO NOTHING: a re-broadcast must not clobber an answer already
            # given. options_json/payload_json stay TEXT on both backends (not
            # jsonb) so a spillover reconcile is a straight copy with no casts.
            await conn.execute(
                f"INSERT INTO pending_asks({cols}) VALUES("
                f"{d.ph()}, {d.ts_param()}, {d.ph(13)}, {d.ts_param()}, {d.ph()}) "
                f"ON CONFLICT (ask_id) DO NOTHING",
                ask_record_to_params(record),
            )

        await d.write(_do)

    async def delete_for_thread(self, thread_id: str) -> int:
        d = self._d

        async def _do(conn):
            cur = await conn.execute(
                f"DELETE FROM pending_asks WHERE thread_id = {d.ph()}", (thread_id,)
            )
            return cur.rowcount or 0

        return await d.write(_do)

    async def resolve(
        self, ask_id: str, response: str, *, status: str = "answered"
    ) -> bool:
        d = self._d

        async def _do(conn):
            # Compare-and-set on status: two surfaces answering at the same
            # instant both call this, and exactly one wins.
            cur = await conn.execute(
                f"UPDATE pending_asks SET status = {d.ph()}, response = {d.ph()}, "
                f"answered_ts = {d.ts_param()} "
                f"WHERE ask_id = {d.ph()} AND status = 'pending'",
                (status, response, time.time(), ask_id),
            )
            return (cur.rowcount or 0) == 1

        return await d.write(_do)

    async def list_pending(self, limit: int = 200) -> list[dict[str, Any]]:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(_ASK_COLS)} FROM pending_asks "
                f"WHERE status = 'pending' ORDER BY created_ts ASC LIMIT {d.ph()}",
                (int(limit),),
            )
            rows = await cur.fetchall()
        return [ask_row_to_record(r) for r in rows]

    async def get(self, ask_id: str) -> dict[str, Any] | None:
        d = self._d
        async with d.reading() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(_ASK_COLS)} FROM pending_asks "
                f"WHERE ask_id = {d.ph()}",
                (ask_id,),
            )
            row = await cur.fetchone()
        return ask_row_to_record(row) if row else None


__all__ = [
    "UnifiedConsentGrantStore",
    "UnifiedConsentRuleStore",
    "UnifiedDecisionStore",
    "UnifiedEventStore",
    "UnifiedPendingAskStore",
    "UnifiedProposalStore",
    "UnifiedToolCounterStore",
]
