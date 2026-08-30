"""Unified events stores match both twins, on both backends.

First two domains of the ``events:*`` batch (Phase 2 step 2.5b):
``ToolCounterStore`` and ``ConsentRuleStore``. Same four-way acceptance shape as
the earlier migrations. It ran against all four implementations — both twins
and the unified store on each dialect, 44 assertions — and the twins were
deleted once they agreed.

These exercise :class:`~yuyutsava.storage.dialect.EventsSqliteDialect`, which
wraps the shared persistent-connection ``SqliteEventsBackend`` rather than a
``BaseSqliteStore``. Getting that right is what unblocks the remaining five
events domains.

Two behaviours are pinned that the twins disagreed on before:

* ``incr`` is a single ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``, so a
  concurrent increment cannot make it return a value it did not write.
* ``match_json`` reads back as a **string** on both backends, though Postgres
  stores it as ``jsonb`` and hands back a ``dict``.

Run:  .venv/bin/python test/storage/test_events_unified_parity.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.storage.models import ConsentRule


def _pg_dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_reachable() -> bool:
    u = urlparse(_pg_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()


def _rule(rid: str, glob: str = "fs.*", decision: str = "auto_approve") -> ConsentRule:
    return ConsentRule(
        rule_id=rid, topic_glob=glob, match_json=json.dumps({"topic": glob, "ext": "pdf"}),
        decision=decision, created_ts=time.time(), expires_ts=None,
    )


class _CounterContract:
    async def test_incr_from_zero(self) -> None:
        self.assertEqual(await self.counters.incr(self.tool, self.day), 1)

    async def test_incr_accumulates(self) -> None:
        for expected in (1, 2, 3):
            self.assertEqual(await self.counters.incr(self.tool, self.day), expected)

    async def test_get_reflects_incr(self) -> None:
        await self.counters.incr(self.tool, self.day)
        await self.counters.incr(self.tool, self.day)
        self.assertEqual(await self.counters.get(self.tool, self.day), 2)

    async def test_get_unknown_is_zero(self) -> None:
        self.assertEqual(await self.counters.get("never-called", self.day), 0)

    async def test_days_are_separate(self) -> None:
        await self.counters.incr(self.tool, self.day)
        self.assertEqual(await self.counters.incr(self.tool, "1999-01-01"), 1)

    async def test_concurrent_incr_loses_nothing(self) -> None:
        """Every increment must be counted, and each caller sees a distinct value.

        The SQLite twin did INSERT-then-SELECT, so its read could observe
        another writer's increment. The single-statement RETURNING form cannot.
        """
        results = await asyncio.gather(
            *(self.counters.incr(self.tool, self.day) for _ in range(5)),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f"concurrent incr raised: {errors}")
        self.assertEqual(await self.counters.get(self.tool, self.day), 5, "an increment was lost")
        self.assertEqual(
            sorted(results), [1, 2, 3, 4, 5],
            f"two callers saw the same count: {sorted(results)}",
        )


class _ConsentRuleContract:
    async def test_put_then_list(self) -> None:
        await self.rules.put(_rule(self.rid("a")))
        listed = [r for r in await self.rules.list() if r.rule_id == self.rid("a")]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].topic_glob, "fs.*")
        self.assertEqual(listed[0].decision, "auto_approve")

    async def test_match_json_reads_back_as_a_string(self) -> None:
        """Postgres stores jsonb and returns a dict; callers expect a str."""
        await self.rules.put(_rule(self.rid("b")))
        rule = next(r for r in await self.rules.list() if r.rule_id == self.rid("b"))
        self.assertIsInstance(
            rule.match_json, str,
            "match_json came back as a non-string; ConsentEvaluator does "
            "json.loads() on it and would fail on a dict",
        )
        self.assertEqual(json.loads(rule.match_json)["ext"], "pdf")

    async def test_reput_is_a_noop_not_an_error(self) -> None:
        await self.rules.put(_rule(self.rid("c")))
        await self.rules.put(_rule(self.rid("c")))
        matching = [r for r in await self.rules.list() if r.rule_id == self.rid("c")]
        self.assertEqual(len(matching), 1)

    async def test_reput_does_not_overwrite(self) -> None:
        """DO NOTHING, not DO UPDATE — the first write wins on both backends."""
        await self.rules.put(_rule(self.rid("d"), glob="fs.*"))
        await self.rules.put(_rule(self.rid("d"), glob="CHANGED", decision="auto_skip"))
        rule = next(r for r in await self.rules.list() if r.rule_id == self.rid("d"))
        self.assertEqual(rule.topic_glob, "fs.*")
        self.assertEqual(rule.decision, "auto_approve")

    async def test_list_is_newest_first(self) -> None:
        # Filter by explicit membership, not a prefix: rid() embeds a per-case
        # suffix on the Postgres side, so rid("e1") is NOT prefixed by rid("e")
        # there. A prefix filter silently matched nothing and the assertion
        # compared [] against the expected pair.
        first, second = self.rid("e1"), self.rid("e2")
        await self.rules.put(_rule(first))
        await asyncio.sleep(0.01)
        await self.rules.put(_rule(second))
        wanted = {first, second}
        mine = [r.rule_id for r in await self.rules.list() if r.rule_id in wanted]
        self.assertEqual(mine, [second, first])




# ---------------------------------------------------------------------------
# proposals / decisions / consent_grants  (playbook order 3, 4, 7)
# ---------------------------------------------------------------------------


class _ProposalContract:
    """``proposals`` — Tier-1 actions awaiting a decision.

    ``try_set_status`` is the interesting one: it is a compare-and-set used to
    make approval single-shot. Two surfaces (the UI and the CLI) can approve the
    same proposal, and only one must win.
    """

    def _proposal(self, pid: str, status: str = "pending"):
        from yuyutsava.storage.models import Proposal

        return Proposal(
            proposal_id=pid, event_id=f"ev-{pid}", topic="fs.changed",
            summary="a file changed", proposed="run the tests",
            subagent="coder", urgency=2, created_ts=1000.0, expires_ts=2000.0,
            status=status, session_id="sess-1", agent_path="orchestrator",
        )

    async def _put_proposal(self, pid: str, status: str = "pending") -> None:
        """Persist the event payload, then the proposal — production's order.

        Postgres carries ``proposals_event_fk ... ON DELETE CASCADE``; SQLite
        has no foreign keys at all in the events schema. Writing the parent
        first is what both the event source and ``task_submission`` already do,
        so the suite follows the real path rather than the laxer one.
        """
        p = self._proposal(pid, status)
        await self._seed_payload(p.event_id, p.topic)
        await self.proposals.put(p)

    async def test_proposal_roundtrip_preserves_every_field(self) -> None:
        pid = self.rid("p-round")
        await self._put_proposal(pid)
        got = await self.proposals.get(pid)
        self.assertIsNotNone(got)
        for field in ("proposal_id", "event_id", "topic", "summary", "proposed",
                      "subagent", "urgency", "created_ts", "expires_ts",
                      "status", "session_id", "agent_path"):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(got, field), getattr(self._proposal(pid), field),
                    f"{field} did not survive the round trip",
                )

    async def test_urgency_stays_an_int(self) -> None:
        """A backend that returns it as a float would break comparisons."""
        pid = self.rid("p-int")
        await self._put_proposal(pid)
        got = await self.proposals.get(pid)
        self.assertIsInstance(got.urgency, int)

    async def test_missing_is_none(self) -> None:
        self.assertIsNone(await self.proposals.get(self.rid("p-nope")))

    async def test_put_is_idempotent(self) -> None:
        pid = self.rid("p-idem")
        await self._put_proposal(pid)
        await self._put_proposal(pid, status="approved")
        got = await self.proposals.get(pid)
        self.assertEqual(
            got.status, "pending",
            "a re-put overwrote the row. ON CONFLICT DO NOTHING means the "
            "FIRST write wins; a redelivered event must not resurrect a "
            "proposal the user already decided on.",
        )

    async def test_try_set_status_is_single_shot(self) -> None:
        """The compare-and-set that keeps approval from firing twice."""
        pid = self.rid("p-cas")
        await self._put_proposal(pid)
        first = await self.proposals.try_set_status(
            pid, from_status="pending", to_status="approved")
        second = await self.proposals.try_set_status(
            pid, from_status="pending", to_status="approved")
        self.assertTrue(first)
        self.assertFalse(
            second,
            "the second approval also won. Two surfaces racing to approve the "
            "same proposal would both run the action.",
        )
        self.assertEqual((await self.proposals.get(pid)).status, "approved")

    async def test_try_set_status_on_a_missing_row_is_false(self) -> None:
        self.assertFalse(await self.proposals.try_set_status(
            self.rid("p-ghost"), from_status="pending", to_status="approved"))


class _DecisionContract:
    """``decisions`` — the resolved-action audit log.

    Timestamps are relative to *now*, not fixed literals. ``list`` orders by
    ``ts DESC`` with no per-case filter, so on the shared Postgres a row stamped
    1970 sorts behind every real decision in the dev database and never appears
    in the first page. The SQLite case, with its private temp DB, cannot show
    that — which is precisely why both backends run the same contract.
    """

    def _assert_ts(self, got: list[float], want: list[float], msg: str = "") -> None:
        """Timestamps agree to 1 microsecond — see test_decisions_list_is_newest_first."""
        self.assertEqual(len(got), len(want), msg or f"{got!r} != {want!r}")
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, delta=1e-6, msg=msg or f"{got!r} != {want!r}")

    @property
    def t0(self) -> float:
        import time as _t

        # Cached per instance so every timestamp in one test shares a base.
        if not hasattr(self, "_t0"):
            self._t0 = _t.time()
        return self._t0

    async def _put(self, *, outcome: str = "approved", ts: float,
                   event_id: str | None = None) -> None:
        await self.decisions.put(
            proposal_id=None, event_id=event_id or self.rid("d-ev"),
            outcome=outcome, action_summary="did the thing", ts=ts,
            session_id="sess-1", agent_path="orchestrator",
        )

    async def _my_decisions(self, limit: int = 50, cursor: float | None = None):
        rows = await self.decisions.list(limit=limit, cursor=cursor)
        return [d for d in rows if self.owns(d.event_id)]

    async def test_decisions_list_is_newest_first(self) -> None:
        for ts in (self.t0 + 1, self.t0 + 3, self.t0 + 2):
            await self._put(ts=ts)
        got = await self._my_decisions()
        # Compared to microsecond tolerance, not exactly. Since migration v20
        # Postgres stores these as TIMESTAMPTZ, whose resolution is 1 us, so an
        # epoch float round-trips rounded (…0348558 -> …034856). No real
        # information is lost — time.time() itself resolves to about a
        # microsecond — but bit-exact equality no longer holds, and a test that
        # demanded it would be asserting float representation, not ordering.
        self._assert_ts(
            [d.ts for d in got], [self.t0 + 3, self.t0 + 2, self.t0 + 1])

    async def test_cursor_pages_strictly_older(self) -> None:
        """Keyset pagination: ``ts < cursor``, so no row is served twice."""
        for ts in (self.t0 + 1, self.t0 + 2, self.t0 + 3):
            await self._put(ts=ts)
        page = await self._my_decisions(cursor=self.t0 + 2)
        self._assert_ts(
            [d.ts for d in page], [self.t0 + 1],
            "the cursor is inclusive on one backend — the boundary row would "
            "be rendered twice while paging",
        )

    async def test_limit_is_honoured(self) -> None:
        for ts in (self.t0 + 1, self.t0 + 2, self.t0 + 3):
            await self._put(ts=ts)
        self.assertLessEqual(len(await self.decisions.list(limit=2)), 2)

    async def test_optional_fields_round_trip(self) -> None:
        ev = self.rid("d-ev")
        await self.decisions.put(
            proposal_id=None, event_id=ev, outcome="skipped",
            action_summary=None, ts=self.t0 + 9, session_id=None, agent_path=None,
        )
        # Tolerance, not equality: TIMESTAMPTZ resolves to 1 us since
        # migration v20, so an epoch float round-trips rounded.
        row = next(d for d in await self._my_decisions()
                   if abs(d.ts - (self.t0 + 9)) < 1e-6)
        self.assertIsNone(row.proposal_id)
        self.assertIsNone(row.action_summary)
        self.assertIsNone(row.session_id)

    async def test_recall_filters_by_topic_glob(self) -> None:
        """``recall`` JOINs event_payloads and glob-matches in Python.

        Both twins matched with ``fnmatchcase`` but built the result dict
        differently — SQLite via ``dict(row)``, Postgres by hand. Same four keys
        either way; this pins that.
        """
        import time as _t

        now = _t.time()
        ev_fs, ev_git = self.rid("d-ev-fs"), self.rid("d-ev-git")
        await self._seed_payload(ev_fs, "fs.changed")
        await self._seed_payload(ev_git, "git.pushed")
        await self._put(ts=now, event_id=ev_fs)
        await self._put(ts=now, event_id=ev_git)

        hits = await self.decisions.recall("fs.*", since_sec=600.0, limit=50)
        topics = {h["topic"] for h in hits}
        self.assertIn("fs.changed", topics)
        self.assertNotIn(
            "git.pushed", topics,
            "the topic glob did not filter — recall would feed unrelated "
            "history into the orchestrator's prompt",
        )
        self.assertEqual(
            set(hits[0]), {"outcome", "action_summary", "ts", "topic"},
            "recall's dict shape differs between backends",
        )

    async def test_recall_respects_the_time_window(self) -> None:
        import time as _t

        ev = self.rid("d-ev-old")
        await self._seed_payload(ev, "fs.changed")
        await self._put(ts=_t.time() - 10_000, event_id=ev)
        hits = await self.decisions.recall("fs.*", since_sec=60.0, limit=50)
        self.assertNotIn(ev, {h.get("event_id") for h in hits})


class _ConsentGrantContract:
    """``consent_grants`` — the durable allowlist read at boot."""

    def _grant(self, gid: str, decision: str = "allow", scope: str = "session"):
        from yuyutsava.consent.models import Grant

        return Grant(
            grant_id=gid, domain="fs", subject_key="write:/tmp",
            decision=decision, scope=scope, scope_ref="sess-1",
            created_ts=1000.0, expires_ts=2000.0,
        )

    async def _my_grants(self):
        return [g for g in await self.grants.load() if self.owns(g.grant_id)]

    async def test_grant_roundtrip_preserves_every_field(self) -> None:
        gid = self.rid("g-round")
        await self.grants.put(self._grant(gid))
        got = next(g for g in await self._my_grants() if g.grant_id == gid)
        for field in ("domain", "subject_key", "decision", "scope",
                      "scope_ref", "created_ts", "expires_ts"):
            with self.subTest(field=field):
                self.assertEqual(getattr(got, field),
                                 getattr(self._grant(gid), field))

    async def test_put_overwrites(self) -> None:
        """Unlike proposals, a grant re-put REPLACES.

        Both twins agreed here (``INSERT OR REPLACE`` / ``DO UPDATE``) and the
        semantics are right: re-granting the same consent with a wider scope
        must take effect, not be silently dropped. Pinned because it is the
        opposite of the proposals rule three classes up, and unifying the two
        into one helper would be exactly the wrong DRY.
        """
        gid = self.rid("g-over")
        await self.grants.put(self._grant(gid, scope="once"))
        await self.grants.put(self._grant(gid, scope="project"))
        got = next(g for g in await self._my_grants() if g.grant_id == gid)
        self.assertEqual(got.scope, "project")

    async def test_delete_removes_only_the_target(self) -> None:
        a, b = self.rid("g-del-a"), self.rid("g-del-b")
        await self.grants.put(self._grant(a))
        await self.grants.put(self._grant(b))
        await self.grants.delete(a)
        left = {g.grant_id for g in await self._my_grants()}
        self.assertNotIn(a, left)
        self.assertIn(b, left)

    async def test_delete_of_a_missing_grant_is_a_no_op(self) -> None:
        await self.grants.delete(self.rid("g-ghost"))

    async def test_expires_ts_may_be_null(self) -> None:
        """A ``project``-scope grant never expires; the column is nullable."""
        from yuyutsava.consent.models import Grant

        gid = self.rid("g-null")
        await self.grants.put(Grant(
            grant_id=gid, domain="fs", subject_key="k", decision="allow",
            scope="project", scope_ref="", created_ts=1000.0, expires_ts=None,
        ))
        got = next(g for g in await self._my_grants() if g.grant_id == gid)
        self.assertIsNone(got.expires_ts)


class _EventPayloadContract:
    """``event_payloads`` — the raw event bodies every other row points at.

    The interesting asymmetry is the JSON column: Postgres stores ``jsonb`` and
    hands back a parsed ``dict``, SQLite stores TEXT and hands back a ``str``.
    Callers expect a ``dict``, so the unified store must normalise — which is
    what ``Dialect.json_value`` is for.
    """

    async def test_payload_comes_back_as_a_dict_on_both_backends(self) -> None:
        eid = self.rid("e-dict")
        await self.events.put_event_payload(
            event_id=eid, topic="fs.changed", ts=1000.0,
            payload={"path": "/tmp/x", "n": 3, "nested": {"a": [1, 2]}},
        )
        got = await self.events.get_event_payload(eid)
        self.assertIsInstance(
            got.payload, dict,
            "the payload came back as a string. jsonb parses, TEXT does not — "
            "callers index into this without checking.",
        )
        self.assertEqual(got.payload["nested"], {"a": [1, 2]})
        self.assertEqual(got.payload["n"], 3)

    async def test_roundtrip_preserves_the_record(self) -> None:
        eid = self.rid("e-round")
        await self.events.put_event_payload(
            event_id=eid, topic="git.pushed", ts=1234.5,
            payload={"k": "v"}, blob_path="/tmp/blob.png",
        )
        got = await self.events.get_event_payload(eid)
        self.assertEqual(got.event_id, eid)
        self.assertEqual(got.topic, "git.pushed")
        self.assertEqual(got.ts, 1234.5)
        self.assertEqual(got.blob_path, "/tmp/blob.png")

    async def test_missing_payload_is_none(self) -> None:
        self.assertIsNone(await self.events.get_event_payload(self.rid("e-nope")))

    async def test_put_replaces(self) -> None:
        """Re-delivering an event updates it — this one is an upsert, not a skip."""
        eid = self.rid("e-replace")
        await self.events.put_event_payload(
            event_id=eid, topic="a", ts=1.0, payload={"v": 1})
        await self.events.put_event_payload(
            event_id=eid, topic="b", ts=2.0, payload={"v": 2})
        got = await self.events.get_event_payload(eid)
        self.assertEqual(got.topic, "b")
        self.assertEqual(got.payload["v"], 2)

    async def test_unserialisable_payload_does_not_raise(self) -> None:
        """``default=str`` is load-bearing: events carry arbitrary objects."""
        from pathlib import Path as _P

        eid = self.rid("e-obj")
        await self.events.put_event_payload(
            event_id=eid, topic="t", ts=1.0, payload={"p": _P("/tmp/x")})
        got = await self.events.get_event_payload(eid)
        self.assertEqual(got.payload["p"], "/tmp/x")

    async def test_sweep_skips_blob_backed_rows(self) -> None:
        """``delete_event_payloads_older_than`` must leave blob rows alone.

        Those are owned by the blob sweep, which ties row removal to unlinking
        the file. Deleting the row here would orphan the file forever.
        """
        plain, blobbed = self.rid("e-plain"), self.rid("e-blob")
        await self.events.put_event_payload(
            event_id=plain, topic="t", ts=100.0, payload={})
        await self.events.put_event_payload(
            event_id=blobbed, topic="t", ts=100.0, payload={}, blob_path="/tmp/b.png")

        await self.events.delete_event_payloads_older_than(500.0)
        self.assertIsNone(await self.events.get_event_payload(plain))
        self.assertIsNotNone(
            await self.events.get_event_payload(blobbed),
            "the TTL sweep deleted a blob-backed row; its file on disk is now "
            "unreachable and will never be cleaned",
        )

    async def test_blob_prefix_sweep_matches_on_prefix(self) -> None:
        keep, drop = self.rid("e-keep"), self.rid("e-drop")
        await self.events.put_event_payload(
            event_id=drop, topic="t", ts=100.0, payload={}, blob_path="/tmp/voice/a.wav")
        await self.events.put_event_payload(
            event_id=keep, topic="t", ts=100.0, payload={}, blob_path="/tmp/visuals/b.png")

        n = await self.events.delete_event_payloads_with_blob_prefix("/tmp/voice/", 500.0)
        self.assertEqual(n, 1)
        self.assertIsNone(await self.events.get_event_payload(drop))
        self.assertIsNotNone(await self.events.get_event_payload(keep))

    async def test_sweeps_respect_the_cutoff(self) -> None:
        eid = self.rid("e-young")
        await self.events.put_event_payload(
            event_id=eid, topic="t", ts=9000.0, payload={})
        await self.events.delete_event_payloads_older_than(500.0)
        self.assertIsNotNone(await self.events.get_event_payload(eid))


class _PendingAskContract:
    """``pending_asks`` — Tier-2 asks the agent is parked on."""

    def _record(self, ask_id: str, **over) -> dict:
        rec = {
            "ask_id": ask_id, "created_ts": 1000.0, "surface": "cli",
            "session_id": "sess-1", "thread_id": self.rid("t-ask"),
            "card_id": None, "task_id": None, "interrupt_id": "int-1",
            "agent_path": "orchestrator", "agent_label": "Orchestrator",
            "title": "Proceed?", "body": "About to delete 3 files",
            "options": ["yes", "no"],
        }
        rec.update(over)
        return rec

    async def _my_asks(self):
        return [a for a in await self.asks.list_pending(limit=500)
                if self.owns(a["ask_id"])]

    async def test_roundtrip_preserves_the_wire_record(self) -> None:
        aid = self.rid("a-round")
        await self.asks.put(self._record(aid))
        got = await self.asks.get(aid)
        self.assertEqual(got["title"], "Proceed?")
        self.assertEqual(got["body"], "About to delete 3 files")
        self.assertEqual(
            got["options"], ["yes", "no"],
            "options came back unparsed. They are stored as a JSON string and "
            "the UI renders them as a list.",
        )
        self.assertEqual(got["agent_label"], "Orchestrator")

    async def test_missing_ask_is_none(self) -> None:
        self.assertIsNone(await self.asks.get(self.rid("a-nope")))

    async def test_ask_put_is_idempotent(self) -> None:
        """Re-broadcasting an ask must not overwrite an answer already given."""
        aid = self.rid("a-idem")
        await self.asks.put(self._record(aid))
        await self.asks.resolve(aid, "yes")
        await self.asks.put(self._record(aid, title="DIFFERENT"))
        got = await self.asks.get(aid)
        self.assertEqual(
            got["title"], "Proceed?",
            "a re-put overwrote a resolved ask — the user's answer would be "
            "discarded and the agent would ask again",
        )

    async def test_resolve_is_single_shot(self) -> None:
        """Two surfaces answering at the same instant: exactly one wins."""
        aid = self.rid("a-cas")
        await self.asks.put(self._record(aid))
        self.assertTrue(await self.asks.resolve(aid, "yes"))
        self.assertFalse(
            await self.asks.resolve(aid, "no"),
            "the second answer also won; the agent would receive whichever "
            "arrived last rather than the one the user actually gave first",
        )

    async def test_resolve_of_a_missing_ask_is_false(self) -> None:
        self.assertFalse(await self.asks.resolve(self.rid("a-ghost"), "yes"))

    async def test_list_pending_excludes_resolved(self) -> None:
        open_id, done_id = self.rid("a-open"), self.rid("a-done")
        await self.asks.put(self._record(open_id))
        await self.asks.put(self._record(done_id))
        await self.asks.resolve(done_id, "yes")
        ids = {a["ask_id"] for a in await self._my_asks()}
        self.assertIn(open_id, ids)
        self.assertNotIn(done_id, ids)

    async def test_list_pending_is_oldest_first(self) -> None:
        """FIFO: the user is shown the question they have been waiting on longest."""
        a, b = self.rid("a-t1"), self.rid("a-t2")
        await self.asks.put(self._record(b, created_ts=2000.0))
        await self.asks.put(self._record(a, created_ts=1000.0))
        ids = [x["ask_id"] for x in await self._my_asks()]
        self.assertLess(ids.index(a), ids.index(b))

    async def test_delete_for_thread_removes_the_conversation_text(self) -> None:
        """Session deletion depends on this — the row holds question + answer."""
        aid = self.rid("a-del")
        await self.asks.put(self._record(aid))
        await self.asks.resolve(aid, "the user's private answer")
        n = await self.asks.delete_for_thread(self.rid("t-ask"))
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(await self.asks.get(aid))

    async def test_delete_for_thread_spares_other_threads(self) -> None:
        mine, other = self.rid("a-mine"), self.rid("a-other")
        await self.asks.put(self._record(mine))
        await self.asks.put(self._record(other, thread_id=self.rid("t-other")))
        await self.asks.delete_for_thread(self.rid("t-ask"))
        self.assertIsNotNone(await self.asks.get(other))


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class _SqliteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.events.sqlite_backend import SqliteEventsBackend

        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "events.db")
        await self.backend.open()
        self.tool, self.day = "tr_read_file", "2026-08-08"

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    def rid(self, suffix: str) -> str:
        return f"rule-{suffix}"

    def owns(self, ident: str) -> bool:
        """Every row in this case's private temp DB is ours."""
        return True

    async def _seed_payload(self, event_id: str, topic: str) -> None:
        """``recall`` JOINs event_payloads, so the parent row must exist."""
        await self.backend.execute(
            "INSERT OR IGNORE INTO event_payloads(event_id, topic, ts, payload_json) "
            "VALUES(?,?,?,?)",
            (event_id, topic, 1000.0, "{}"),
        )


class SqliteUnified(
    _CounterContract, _ConsentRuleContract,
    _ProposalContract, _DecisionContract, _ConsentGrantContract,
    _EventPayloadContract, _PendingAskContract,
    _SqliteCase,
):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import (
            UnifiedConsentGrantStore, UnifiedConsentRuleStore, UnifiedDecisionStore,
            UnifiedEventStore, UnifiedPendingAskStore, UnifiedProposalStore,
            UnifiedToolCounterStore,
        )

        d = EventsSqliteDialect(self.backend)
        self.counters = UnifiedToolCounterStore(d)
        self.rules = UnifiedConsentRuleStore(d)
        self.proposals = UnifiedProposalStore(d)
        self.decisions = UnifiedDecisionStore(d)
        self.grants = UnifiedConsentGrantStore(d)
        self.events = UnifiedEventStore(d)
        self.asks = UnifiedPendingAskStore(d)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class _PgCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.tool = f"tool-{self._suffix}"
        self.day = "2026-08-08"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM tool_call_counters WHERE tool_name = %s", (self.tool,))
            await conn.execute("DELETE FROM consent_rules WHERE rule_id LIKE %s", (f"rule-%{self._suffix}",))
            # Shared PG instance: every row this case wrote carries the suffix.
            like = f"rule-%{self._suffix}"
            await conn.execute("DELETE FROM decisions WHERE event_id LIKE %s", (like,))
            await conn.execute("DELETE FROM event_payloads WHERE event_id LIKE %s", (like,))
            await conn.execute("DELETE FROM proposals WHERE proposal_id LIKE %s", (like,))
            await conn.execute("DELETE FROM consent_grants WHERE grant_id LIKE %s", (like,))
            await conn.execute("DELETE FROM pending_asks WHERE ask_id LIKE %s", (like,))
        await self.pool.close()

    def rid(self, suffix: str) -> str:
        return f"rule-{suffix}-{self._suffix}"

    def owns(self, ident: str) -> bool:
        """Postgres is shared, so rows must be filtered to this case.

        By SUFFIX, not prefix: ``rid`` appends the per-case marker, so
        ``rid("g")`` is not a prefix of ``rid("g-del-b")``. A ``startswith``
        filter here silently matches nothing and every assertion reads as
        "the store lost the row".
        """
        return ident.endswith(self._suffix)

    async def _seed_payload(self, event_id: str, topic: str) -> None:
        """``recall`` JOINs event_payloads, so the parent row must exist."""
        async with self.pool.connection() as conn:
            await conn.execute(
                # ts is TIMESTAMPTZ since migration v20.
                "INSERT INTO event_payloads(event_id, topic, ts, payload_json) "
                "VALUES(%s,%s,to_timestamp(%s),%s) ON CONFLICT (event_id) DO NOTHING",
                (event_id, topic, 1000.0, "{}"),
            )


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnified(
    _CounterContract, _ConsentRuleContract,
    _ProposalContract, _DecisionContract, _ConsentGrantContract,
    _EventPayloadContract, _PendingAskContract,
    _PgCase,
):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.storage.dialect import PostgresDialect
        from yuyutsava.storage.events.unified import (
            UnifiedConsentGrantStore, UnifiedConsentRuleStore, UnifiedDecisionStore,
            UnifiedEventStore, UnifiedPendingAskStore, UnifiedProposalStore,
            UnifiedToolCounterStore,
        )

        d = PostgresDialect(self.pool)
        self.counters = UnifiedToolCounterStore(d)
        self.rules = UnifiedConsentRuleStore(d)
        self.proposals = UnifiedProposalStore(d)
        self.decisions = UnifiedDecisionStore(d)
        self.grants = UnifiedConsentGrantStore(d)
        self.events = UnifiedEventStore(d)
        self.asks = UnifiedPendingAskStore(d)



class ContractMixinsDoNotCollide(unittest.TestCase):
    """No two contract mixins may define the same method name.

    Found the hard way. ``_DecisionContract`` and ``_ConsentGrantContract`` both
    defined ``_mine`` and ``test_roundtrip_preserves_every_field``. Python
    resolves that by MRO order, so the grant versions were silently **shadowed**:
    the grant round-trip test never ran at all, and the grant tests that did run
    called the *decisions* helper and saw an empty list.

    Nothing failed in a way that pointed at the cause — the assertions read as
    "the store lost the row". A suite that quietly stops testing a domain is
    worse than one that fails, so the collision is now an error.
    """

    def test_no_duplicate_method_names_across_mixins(self) -> None:
        import ast
        from pathlib import Path as _P

        tree = ast.parse(_P(__file__).read_text(encoding="utf-8"))
        owner: dict[str, str] = {}
        collisions: list[str] = []
        for node in tree.body:
            if not (isinstance(node, ast.ClassDef)
                    and node.name.startswith("_") and node.name.endswith("Contract")):
                continue
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if member.name in owner:
                    collisions.append(
                        f"{member.name}: defined by both {owner[member.name]} "
                        f"and {node.name}"
                    )
                owner[member.name] = node.name
        self.assertEqual(
            collisions, [],
            "contract mixins share a method name; MRO order decides which one "
            "runs and the other is silently dropped:\n  "
            + "\n  ".join(collisions),
        )

    def test_every_contract_is_mounted_on_both_backends(self) -> None:
        """A mixin nobody inherits tests nothing — and looks like it does."""
        import ast
        from pathlib import Path as _P

        tree = ast.parse(_P(__file__).read_text(encoding="utf-8"))
        contracts = {n.name for n in tree.body
                     if isinstance(n, ast.ClassDef)
                     and n.name.startswith("_") and n.name.endswith("Contract")}
        mounted = {
            case: {b.id for b in n.bases if isinstance(b, ast.Name)}
            for n in tree.body if isinstance(n, ast.ClassDef)
            for case in [n.name] if case in ("SqliteUnified", "PostgresUnified")
        }
        for case, bases in mounted.items():
            with self.subTest(case=case):
                self.assertEqual(
                    contracts - bases, set(),
                    f"{case} does not run: {sorted(contracts - bases)}. A domain "
                    f"verified on one backend only is exactly the divergence "
                    f"this suite exists to prevent.",
                )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
