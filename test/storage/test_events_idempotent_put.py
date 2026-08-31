"""A repeated ``put`` is a no-op on both backends, not a crash on one.

Found while sizing the ``events:*`` migration batch (Phase 2 step 2.5b), before
any migration code was written.

Every Postgres ``put`` in ``events/pg_stores.py`` carries
``ON CONFLICT (...) DO NOTHING``. Three of their SQLite twins used a plain
``INSERT``, so the same call that Postgres silently ignored raised
``IntegrityError`` on SQLite:

    ProposalStore.put      ON CONFLICT (proposal_id) DO NOTHING   vs  plain INSERT
    DecisionStore.put      ON CONFLICT (decision_id) DO NOTHING   vs  plain INSERT
    ConsentRuleStore.put   ON CONFLICT (rule_id)     DO NOTHING   vs  plain INSERT

``ConsentRuleStore`` is the one that bites, via a **retry**: re-putting the same
rule was a no-op on Postgres and an ``IntegrityError`` on SQLite, the default
backend. (An earlier note here claimed ``rule_id`` is caller-chosen — it is not;
``triage_loop.py:387`` mints a fresh ULID, so distinct rules never collide.)

``DO NOTHING`` is correct *because* of that: a conflict can only mean the same
rule twice, and consent rules have no update path — the only other mutation is
``DELETE ... WHERE rule_id=?``. If ``rule_id`` ever becomes content-derived or
user-supplied, this must become an explicit ``DO UPDATE``, or a real edit will
vanish silently.

``ProposalStore`` is reachable on a retry. ``DecisionStore`` mints a fresh ULID
per call, so a collision is not reachable today; its clause is defensive parity
so the two backends cannot drift apart later.

Four other events puts were flagged by a first pass and are **not** bugs — they
already use SQLite's own idempotent syntax (``INSERT OR REPLACE`` /
``INSERT OR IGNORE``) or ``ON CONFLICT ... DO UPDATE``. Checking that before
reporting is why the number here is 3 and not 6.

Run:  .venv/bin/python test/storage/test_events_idempotent_put.py
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yuyutsava.storage.dialect import EventsSqliteDialect
from yuyutsava.storage.events.sqlite_backend import (
    SqliteEventsBackend,
)
from yuyutsava.storage.events.unified import (
    UnifiedConsentRuleStore, UnifiedDecisionStore, UnifiedProposalStore,
)
from yuyutsava.storage.models import ConsentRule, Proposal


def _proposal(pid: str = "p1") -> Proposal:
    now = time.time()
    return Proposal(
        proposal_id=pid, event_id="e1", topic="fs.write", summary="s",
        proposed="do", subagent="a", urgency=1, created_ts=now, expires_ts=now + 60,
        status="pending", session_id=None, agent_path=None,
    )


def _rule(rid: str = "r1") -> ConsentRule:
    return ConsentRule(
        rule_id=rid, topic_glob="fs.*", match_json='{"a": 1}',
        decision="auto_approve", created_ts=time.time(), expires_ts=None,
    )


class SqlitePutIsIdempotent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "events.db")
        await self.backend.open()

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def test_consent_rule_reput_does_not_raise(self) -> None:
        """A retried put of the same rule is a no-op, not a crash."""
        store = UnifiedConsentRuleStore(EventsSqliteDialect(self.backend))
        await store.put(_rule())
        await store.put(_rule())  # raised IntegrityError before this fix
        self.assertEqual(len(await store.list()), 1, "the duplicate was inserted")

    async def test_consent_rule_first_write_wins(self) -> None:
        """DO NOTHING, not DO UPDATE — the original row must survive intact."""
        store = UnifiedConsentRuleStore(EventsSqliteDialect(self.backend))
        await store.put(_rule())
        second = ConsentRule(
            rule_id="r1", topic_glob="CHANGED", match_json="{}",
            decision="auto_skip", created_ts=time.time(), expires_ts=None,
        )
        await store.put(second)
        rules = await store.list()
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0].topic_glob, "fs.*",
            "a repeated put overwrote the existing rule; Postgres does NOT "
            "overwrite (DO NOTHING), so this would be a new divergence",
        )

    async def test_proposal_reput_does_not_raise(self) -> None:
        store = UnifiedProposalStore(EventsSqliteDialect(self.backend))
        # Schema v5 gave proposals an FK to event_payloads, so the parent must
        # exist — which is the order every producer already uses
        # (events/source.py persists the payload before publishing).
        await self.backend.execute(
            "INSERT INTO event_payloads(event_id, topic, ts, payload_json) "
            "VALUES(?,?,?,?)", ("e1", "fs.write", 1.0, "{}"),
        )
        await store.put(_proposal())
        await store.put(_proposal())
        self.assertIsNotNone(await store.get("p1"))

    async def test_decision_put_still_appends(self) -> None:
        """Decisions mint a fresh id per call — the clause must not suppress them."""
        store = UnifiedDecisionStore(EventsSqliteDialect(self.backend))
        await store.put(proposal_id=None, event_id="e1", outcome="ok")
        await store.put(proposal_id=None, event_id="e1", outcome="ok")
        self.assertEqual(
            len(await store.list(limit=10)), 2,
            "ON CONFLICT(decision_id) swallowed a distinct decision — each put "
            "mints its own ULID, so both rows must land",
        )


class BackendParity(unittest.TestCase):
    """Both backends must agree on which puts are idempotent."""

    def test_every_pg_put_with_on_conflict_has_an_idempotent_sqlite_twin(self) -> None:
        import inspect
        import re
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_twin_conformance import _all_twins, _public_async_methods

        def idempotent(src: str, is_sqlite: bool) -> bool:
            upper = src.upper()
            if "ON CONFLICT" in upper:
                return True
            return bool(is_sqlite and re.search(r"INSERT\s+OR\s+(REPLACE|IGNORE)", upper))

        # Only the events pairs that still HAVE twins. ConsentRuleStore and
        # ToolCounterStore were collapsed onto the dialect adapter on
        # 2026-08-08, so they no longer appear in _all_twins() — one
        # implementation cannot diverge from itself.
        offenders: list[str] = []
        for label, _iface, sq, pg in _all_twins():
            if not label.startswith("events:"):
                continue
            for name in sorted(set(_public_async_methods(sq)) & set(_public_async_methods(pg))):
                try:
                    s_src = inspect.getsource(getattr(sq, name))
                    p_src = inspect.getsource(getattr(pg, name))
                except (OSError, TypeError):
                    continue
                if not re.search(r"\bINSERT\b", s_src, re.I):
                    continue
                if idempotent(p_src, False) and not idempotent(s_src, True):
                    offenders.append(f"{label}.{name}")

        self.assertEqual(
            offenders, [],
            "Postgres ignores a duplicate here but SQLite raises IntegrityError:\n  "
            + "\n  ".join(offenders)
            + "\n\nSQLite supports ON CONFLICT DO NOTHING (3.24+); use it, or "
              "INSERT OR IGNORE.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
