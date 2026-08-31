"""SQLite events tables enforce the foreign keys Postgres always had.

Schema v5, closing **finding AC**.

Postgres carried `proposals_event_fk ... ON DELETE CASCADE` and
`decisions_proposal_fk ... ON DELETE SET NULL`. The SQLite events schema had no
`REFERENCES` clause at all, so the 7-day `event_payloads` sweep collected
proposals on Postgres and left them forever on SQLite — the default,
zero-config backend. This suite pins the fix, and the three things that make it
more than a schema edit:

1. **Enforcement.** SQLite defaults `PRAGMA foreign_keys` to OFF. Without it
   turned on, a `REFERENCES` clause parses, stores, and does nothing —
   which is a *worse* state than no constraint, because the schema now claims
   an invariant it does not hold.
2. **The migration.** SQLite has no `ALTER TABLE ADD CONSTRAINT`, so existing
   databases need both tables rebuilt, and existing rows can already violate
   the new constraint.
3. **The spillover buffer.** In Postgres mode these same tables are a write
   buffer, and the reconciler deletes drained parent rows while child rows are
   still waiting to drain. Cascade there destroys data during outage recovery —
   the failure this suite's last class exists to catch.

Run:  .venv/bin/python test/storage/test_events_foreign_keys.py
"""

from __future__ import annotations

import contextlib
import tempfile
import time
import unittest
from pathlib import Path

import aiosqlite

from yuyutsava.storage.events.sqlite_backend import SqliteEventsBackend
from yuyutsava.storage.models import Proposal


def _proposal(pid: str, event_id: str) -> Proposal:
    now = time.time()
    return Proposal(
        proposal_id=pid, event_id=event_id, topic="fs.changed", summary="s",
        proposed="do it", subagent="coder", urgency=1, created_ts=now,
        expires_ts=now + 60, status="pending", session_id="sess-1",
        agent_path="orchestrator",
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.db"
        self.backend = SqliteEventsBackend(self.path)
        await self.backend.open()

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def _seed(self, event_id: str) -> None:
        await self.backend.execute(
            "INSERT INTO event_payloads(event_id, topic, ts, payload_json) "
            "VALUES(?,?,?,?)",
            (event_id, "fs.changed", time.time(), "{}"),
        )


class EnforcementIsOn(_Base):
    """The pragma, without which the constraints are decorative."""

    async def test_pragma_is_enabled_on_the_connection(self) -> None:
        rows = await self.backend.fetchall("PRAGMA foreign_keys")
        self.assertEqual(
            rows[0][0], 1,
            "PRAGMA foreign_keys is OFF, so every REFERENCES clause in "
            "events/schema.py is inert and the schema claims an invariant it "
            "does not enforce",
        )

    async def test_both_constraints_are_present(self) -> None:
        for table, expect in (("proposals", "event_payloads"), ("decisions", "proposals")):
            with self.subTest(table=table):
                rows = await self.backend.fetchall(f"PRAGMA foreign_key_list({table})")
                self.assertTrue(rows, f"{table} has no foreign key")
                self.assertEqual(rows[0]["table"], expect)

    async def test_orphan_proposal_is_rejected(self) -> None:
        """A proposal for an event that was never persisted must not insert."""
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import UnifiedProposalStore

        store = UnifiedProposalStore(EventsSqliteDialect(self.backend))
        with self.assertRaises(aiosqlite.IntegrityError):
            await store.put(_proposal("p-orphan", "ev-never-existed"))


class CascadeBehaviour(_Base):
    """What the FK actually buys: retention parity with Postgres."""

    async def test_sweeping_an_event_removes_its_proposals(self) -> None:
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import (
            UnifiedEventStore, UnifiedProposalStore,
        )

        d = EventsSqliteDialect(self.backend)
        events, proposals = UnifiedEventStore(d), UnifiedProposalStore(d)

        await events.put_event_payload(
            event_id="ev-old", topic="fs.changed", ts=100.0, payload={})
        await proposals.put(_proposal("p-old", "ev-old"))
        self.assertIsNotNone(await proposals.get("p-old"))

        # The 7-day sweep, which is what Postgres has always cascaded on.
        await events.delete_event_payloads_older_than(500.0)

        self.assertIsNone(
            await proposals.get("p-old"),
            "the proposal outlived the event it belongs to. This is finding AC: "
            "Postgres collects these via cascade and SQLite grew forever.",
        )

    async def test_decisions_survive_with_a_null_proposal(self) -> None:
        """SET NULL, not CASCADE — the audit record must outlive the proposal."""
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import (
            UnifiedDecisionStore, UnifiedEventStore, UnifiedProposalStore,
        )

        d = EventsSqliteDialect(self.backend)
        events = UnifiedEventStore(d)
        await events.put_event_payload(
            event_id="ev-d", topic="t", ts=100.0, payload={})
        await UnifiedProposalStore(d).put(_proposal("p-d", "ev-d"))
        await UnifiedDecisionStore(d).put(
            proposal_id="p-d", event_id="ev-d", outcome="approved",
            action_summary="ran it", ts=time.time(),
        )

        await events.delete_event_payloads_older_than(500.0)

        rows = await self.backend.fetchall(
            "SELECT decision_id, proposal_id FROM decisions WHERE event_id='ev-d'")
        self.assertEqual(
            len(rows), 1,
            "the decision was deleted along with its proposal. Decisions are the "
            "audit log — losing them means no record that the action ran.",
        )
        self.assertIsNone(
            rows[0]["proposal_id"],
            "the decision kept a proposal_id pointing at a deleted row",
        )

    async def test_a_live_event_keeps_its_proposals(self) -> None:
        """The cascade must not fire on rows the sweep did not touch."""
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import (
            UnifiedEventStore, UnifiedProposalStore,
        )

        d = EventsSqliteDialect(self.backend)
        events, proposals = UnifiedEventStore(d), UnifiedProposalStore(d)
        await events.put_event_payload(
            event_id="ev-new", topic="t", ts=9000.0, payload={})
        await proposals.put(_proposal("p-new", "ev-new"))

        await events.delete_event_payloads_older_than(500.0)
        self.assertIsNotNone(await proposals.get("p-new"))


class MigrationFromV4(unittest.IsolatedAsyncioTestCase):
    """An existing database gains the constraints — and its violating rows resolve."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.db"
        await self._build_v4_db()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _build_v4_db(self) -> None:
        """A pre-v5 database: no foreign keys, and already holding orphans."""
        conn = await aiosqlite.connect(str(self.path))
        await conn.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE event_payloads (
                event_id TEXT PRIMARY KEY, topic TEXT NOT NULL, ts REAL NOT NULL,
                payload_json TEXT NOT NULL, blob_path TEXT);
            CREATE TABLE proposals (
                proposal_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                topic TEXT NOT NULL, summary TEXT NOT NULL, proposed TEXT NOT NULL,
                subagent TEXT NOT NULL, urgency INTEGER NOT NULL,
                created_ts REAL NOT NULL, expires_ts REAL NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending','approved','skipped','expired','modified')),
                session_id TEXT, agent_path TEXT);
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY, proposal_id TEXT, event_id TEXT NOT NULL,
                outcome TEXT NOT NULL, action_summary TEXT, ts REAL NOT NULL,
                session_id TEXT, agent_path TEXT);
            INSERT INTO schema_meta VALUES('version','4');
            INSERT INTO event_payloads VALUES('ev-live','t',100.0,'{}',NULL);
            -- kept: its parent exists
            INSERT INTO proposals VALUES('p-live','ev-live','t','s','p','a',1,1.0,2.0,'pending',NULL,NULL);
            -- dropped: parent was swept long ago, exactly the drift being closed
            INSERT INTO proposals VALUES('p-orphan','ev-gone','t','s','p','a',1,1.0,2.0,'pending',NULL,NULL);
            INSERT INTO decisions VALUES('d-live','p-live','ev-live','approved',NULL,1.0,NULL,NULL);
            -- proposal_id nulled: the decision itself must survive
            INSERT INTO decisions VALUES('d-orphan','p-orphan','ev-gone','approved',NULL,1.0,NULL,NULL);
        """)
        await conn.commit()
        await conn.close()

    @contextlib.asynccontextmanager
    async def _opened(self):
        backend = SqliteEventsBackend(self.path)
        await backend.open()
        try:
            yield backend
        finally:
            await backend.close()

    async def test_constraints_are_added_to_an_existing_db(self) -> None:
        async with self._opened() as b:
            for table in ("proposals", "decisions"):
                with self.subTest(table=table):
                    self.assertTrue(
                        await b.fetchall(f"PRAGMA foreign_key_list({table})"),
                        f"{table} still has no FK after migrating; SQLite has no "
                        f"ALTER TABLE ADD CONSTRAINT, so the table must be rebuilt",
                    )

    async def test_valid_rows_survive_the_rebuild(self) -> None:
        async with self._opened() as b:
            rows = await b.fetchall("SELECT proposal_id FROM proposals")
            self.assertEqual(
                {r["proposal_id"] for r in rows}, {"p-live"},
                "the rebuild lost a proposal whose event still exists",
            )

    async def test_orphan_proposals_are_dropped(self) -> None:
        """They cannot be kept: ``event_id`` is NOT NULL, so there is no null to set.

        Postgres had already removed the equivalent rows via its cascade, so
        dropping them is what makes the two backends agree.
        """
        async with self._opened() as b:
            rows = await b.fetchall(
                "SELECT proposal_id FROM proposals WHERE proposal_id='p-orphan'")
            self.assertEqual(rows, [])

    async def test_orphan_decisions_are_nulled_not_deleted(self) -> None:
        async with self._opened() as b:
            rows = await b.fetchall(
                "SELECT proposal_id FROM decisions WHERE decision_id='d-orphan'")
            self.assertEqual(len(rows), 1, "an audit record was destroyed by the rebuild")
            self.assertIsNone(rows[0]["proposal_id"])

    async def test_migration_is_idempotent(self) -> None:
        """Reopening must not rebuild again — the gate reads the schema, not the version."""
        async with self._opened() as b:
            first = await b.fetchall("SELECT proposal_id FROM proposals")
        async with self._opened() as b:
            second = await b.fetchall("SELECT proposal_id FROM proposals")
            self.assertEqual(
                {r["proposal_id"] for r in first}, {r["proposal_id"] for r in second})
            leftovers = await b.fetchall(
                "SELECT name FROM sqlite_master WHERE name LIKE '%\\_v5' ESCAPE '\\'")
            self.assertEqual(leftovers, [], "a rebuild scratch table was left behind")

    async def test_no_violations_survive(self) -> None:
        async with self._opened() as b:
            self.assertEqual(await b.fetchall("PRAGMA foreign_key_check"), [])


class FreshDatabaseIsNotRebuilt(_Base):
    """A first boot must not rebuild tables that were already correct.

    ``schema_meta`` is written at the *end* of ``migrate``, so a brand-new
    database reports ``current = 0`` even though ``SCHEMA_SQL`` just created both
    tables with their constraints. Gating the rebuild on the version anchor alone
    would therefore rebuild two tables on every single first boot.

    Detected via ``sqlite_master.rootpage``, which changes when a table is
    dropped and recreated. An earlier version of this class asserted only that
    no scratch tables were left behind — which a redundant rebuild also
    satisfies, since it cleans up after itself. That test passed with the gate
    deleted, i.e. it tested nothing. ``rootpage`` is the observable that
    actually distinguishes "skipped" from "rebuilt and tidied".
    """

    async def _rootpages(self) -> dict:
        rows = await self.backend.fetchall(
            "SELECT name, rootpage FROM sqlite_master "
            "WHERE type='table' AND name IN ('proposals','decisions')")
        return {r["name"]: r["rootpage"] for r in rows}

    async def test_the_rebuild_is_skipped_entirely(self) -> None:
        from yuyutsava.storage.events.schema import _add_foreign_keys_v5

        before = await self._rootpages()
        await _add_foreign_keys_v5(self.backend._c)
        self.assertEqual(
            await self._rootpages(), before,
            "the tables were dropped and recreated even though they already "
            "carried their constraints — that would happen on every first boot",
        )

    async def test_a_rebuild_would_change_the_rootpage(self) -> None:
        """Proves the probe above can actually detect a rebuild.

        Without this, ``test_the_rebuild_is_skipped_entirely`` could be passing
        because ``rootpage`` never moves, not because the rebuild was skipped.
        """
        before = await self._rootpages()
        await self.backend.execute(
            "CREATE TABLE decisions_probe AS SELECT * FROM decisions")
        await self.backend.execute("DROP TABLE decisions")
        await self.backend.execute("ALTER TABLE decisions_probe RENAME TO decisions")
        self.assertNotEqual(
            (await self._rootpages()).get("decisions"), before["decisions"],
            "rootpage did not move across a real drop+recreate, so it cannot "
            "be used to detect a rebuild",
        )

    async def test_no_scratch_tables_remain(self) -> None:
        rows = await self.backend.fetchall(
            "SELECT name FROM sqlite_master WHERE name LIKE '%\\_v5' ESCAPE '\\'")
        self.assertEqual(rows, [])

    async def test_version_anchor_is_current(self) -> None:
        from yuyutsava.storage.events.schema import SCHEMA_VERSION

        rows = await self.backend.fetchall(
            "SELECT value FROM schema_meta WHERE key='version'")
        self.assertEqual(int(rows[0]["value"]), SCHEMA_VERSION)


class ReconcilerIsNotEatenByTheCascade(_Base):
    """**The hazard the FK introduced**, and the reason for ``foreign_keys_off``.

    In Postgres mode these tables are a spillover *buffer*. The reconciler drains
    parents first — Postgres needs the parent row before the child — and deletes
    each drained batch from the buffer as it goes. With cascade live, deleting a
    drained ``event_payloads`` row takes that event's still-undrained
    ``proposals`` with it, and they never reach Postgres.

    Silent, and only during an outage recovery. This test fails without the
    suspension.
    """

    async def test_buffered_proposals_reach_postgres(self) -> None:
        from yuyutsava.storage.dialect import EventsSqliteDialect
        from yuyutsava.storage.events.unified import (
            UnifiedEventStore, UnifiedProposalStore,
        )
        from yuyutsava.storage.routing.reconcile import Reconciler

        d = EventsSqliteDialect(self.backend)
        await UnifiedEventStore(d).put_event_payload(
            event_id="ev-buf", topic="t", ts=time.time(), payload={"a": 1})
        await UnifiedProposalStore(d).put(_proposal("p-buf", "ev-buf"))

        captured: list = []

        class _Conn:
            async def execute(self, sql, params=None):
                captured.append((sql, params))

        class _Pool:
            @contextlib.asynccontextmanager
            async def connection(self):
                yield _Conn()

        moved = await Reconciler(self.backend, _Pool()).reconcile()

        tables = " ".join(sql for sql, _ in captured)
        self.assertIn("event_payloads", tables)
        self.assertIn(
            "proposals", tables,
            "the buffered proposal never reached Postgres. Deleting the drained "
            "event_payloads row cascaded it away first — data lost during outage "
            "recovery, which is exactly when it is least noticed.",
        )
        self.assertEqual(moved, 2)
        self.assertEqual(
            await self.backend.fetchall("SELECT proposal_id FROM proposals"), [],
            "drain-and-delete: the buffer must be empty afterwards",
        )

    async def test_enforcement_is_restored_afterwards(self) -> None:
        """A suspension that leaked would silently disable the FK for the process."""
        from yuyutsava.storage.routing.reconcile import Reconciler

        class _Pool:
            @contextlib.asynccontextmanager
            async def connection(self):
                raise AssertionError("should not be reached")
                yield  # pragma: no cover

        await Reconciler(self.backend, _Pool()).reconcile()
        rows = await self.backend.fetchall("PRAGMA foreign_keys")
        self.assertEqual(
            rows[0][0], 1,
            "foreign keys were left OFF after reconcile — every later write "
            "would skip the constraint",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
