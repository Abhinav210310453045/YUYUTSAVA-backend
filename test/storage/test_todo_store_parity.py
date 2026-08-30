"""``UnifiedTodoStore`` behaves identically on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 15 — the last and largest twin pair (804
lines, 20 methods, 5 tables). Written **before** the unified store.

The TODO board holds **real user data**, so the Postgres cases suffix every id
with a per-run marker and delete exactly those rows in teardown. Nothing here
queries or mutates a card it did not create.

Five behaviours carry the weight and get most of the coverage:

* **Child cascade.** Postgres has four ``ON DELETE CASCADE`` foreign keys onto
  ``todo_cards``; SQLite has none and deletes the same four tables by hand
  (``PRAGMA foreign_keys`` is off on that connection). Same outcome, two
  mechanisms — so the *outcome* is what gets asserted, on both.
* **Parent-existence checks.** ``add_note``/``add_objective``/``add_attachment``
  return ``False`` for an unknown card rather than raising. On Postgres an FK
  would raise; the explicit check is what makes the two agree.
* **`updated_ts` bumping.** Writing a child touches the card's ``updated_ts``,
  which is what orders the board. A missed bump sends a just-edited card to the
  bottom of the list.
* **Field whitelists.** ``update_card``/``update_objective`` interpolate column
  names, so ``_CARD_UPDATE_FIELDS`` / ``_OBJECTIVE_UPDATE_FIELDS`` are the
  injection boundary.
* **`pinned` and `tags` coercion.** ``pinned`` is INTEGER on **both**
  backends — passing a Python ``bool`` is a type error on Postgres — while
  callers see a ``bool``. ``tags``/``meta``/``payload`` are TEXT vs ``jsonb``.

Run:  .venv/bin/python test/storage/test_todo_store_parity.py
"""

from __future__ import annotations

# NOTE: helpers below use the raw pool connection, which yields TUPLES.
# They read positionally on purpose — reading by name only worked while the
# dialect was leaking `dict_row` onto pooled connections (finding AT).

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN


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


class _TodoContract:
    """Behaviour both backends must agree on."""

    # -- builders -----------------------------------------------------------

    def _card(self, cid: str, **over):
        from yuyutsava.todoboard.models import TodoCardV1

        now = time.time()
        base = dict(
            card_id=cid, title="a card", status="inbox", pinned=False,
            tags=["alpha", "beta"], workspace_path="/tmp/ws",
            created_ts=now, updated_ts=now,
        )
        base.update(over)
        return TodoCardV1(**base)

    def _note(self, nid: str, cid: str, **over):
        from yuyutsava.todoboard.models import TodoNoteV1

        now = time.time()
        base = dict(note_id=nid, card_id=cid, body="a note", author="user",
                    objective_id=None, phase=None, created_ts=now, updated_ts=now)
        base.update(over)
        return TodoNoteV1(**base)

    def _objective(self, oid: str, cid: str, **over):
        from yuyutsava.todoboard.models import TodoObjectiveV1

        now = time.time()
        base = dict(objective_id=oid, card_id=cid, title="a step",
                    phase="thinking", order_idx=0, reason=None, outcome=None,
                    created_ts=now, updated_ts=now)
        base.update(over)
        return TodoObjectiveV1(**base)

    def _attachment(self, aid: str, cid: str, **over):
        from yuyutsava.todoboard.models import TodoAttachmentV1

        base = dict(attachment_id=aid, card_id=cid, kind="file",
                    path="/tmp/x.png", url=None, mime="image/png",
                    title="shot", meta={"w": 10, "h": 20}, created_ts=time.time())
        base.update(over)
        return TodoAttachmentV1(**base)

    def _event(self, eid: str, cid: str, **over):
        from yuyutsava.todoboard.models import TodoEventV1

        base = dict(event_id=eid, card_id=cid, objective_id=None,
                    kind="note_added", payload={"n": 1}, actor="user",
                    created_ts=time.time())
        base.update(over)
        return TodoEventV1(**base)

    async def _seed_card(self, name: str):
        cid = self.cid(name)
        await self.store.add_card(self._card(cid))
        return cid

    # -- cards --------------------------------------------------------------

    async def test_add_then_get_card(self) -> None:
        cid = await self._seed_card("get")
        got = await self.store.get_card(cid)
        self.assertEqual(got.card_id, cid)
        self.assertEqual(got.title, "a card")
        self.assertEqual(got.workspace_path, "/tmp/ws")

    async def test_tags_round_trip_as_a_list(self) -> None:
        """``tags`` is jsonb on Postgres and TEXT on SQLite."""
        cid = await self._seed_card("tags")
        got = await self.store.get_card(cid)
        self.assertEqual(got.tags, ["alpha", "beta"])

    async def test_empty_tags_round_trip(self) -> None:
        cid = self.cid("notags")
        await self.store.add_card(self._card(cid, tags=[]))
        self.assertEqual((await self.store.get_card(cid)).tags, [])

    async def test_pinned_round_trips_as_a_bool(self) -> None:
        """Stored as INTEGER on both backends; callers get a real ``bool``."""
        cid = self.cid("pin")
        await self.store.add_card(self._card(cid, pinned=True))
        got = await self.store.get_card(cid)
        self.assertIs(got.pinned, True)

    async def test_timestamps_are_epoch_floats(self) -> None:
        before = time.time()
        cid = await self._seed_card("ts")
        got = await self.store.get_card(cid)
        self.assertIsInstance(got.created_ts, float)
        self.assertGreaterEqual(got.created_ts, before - 5)

    async def test_missing_card_is_none(self) -> None:
        self.assertIsNone(await self.store.get_card(self.cid("ghost")))

    async def test_update_card_fields(self) -> None:
        cid = await self._seed_card("upd")
        ok = await self.store.update_card(
            cid, {"title": "renamed", "status": "active", "pinned": True,
                  "tags": ["x"], "updated_ts": time.time()})
        self.assertTrue(ok)
        got = await self.store.get_card(cid)
        self.assertEqual(got.title, "renamed")
        self.assertEqual(got.status, "active")
        self.assertIs(got.pinned, True)
        self.assertEqual(got.tags, ["x"])

    async def test_update_card_ignores_unknown_fields(self) -> None:
        """The whitelist is the injection boundary — unknown keys are dropped."""
        cid = await self._seed_card("wl")
        ok = await self.store.update_card(
            cid, {"title": "kept", "card_id = 'x' --": "boom", "created_ts": 0.0})
        self.assertTrue(ok)
        got = await self.store.get_card(cid)
        self.assertEqual(got.title, "kept")
        self.assertNotEqual(got.created_ts, 0.0, "created_ts is not updatable")

    async def test_update_card_with_no_valid_fields_is_a_noop(self) -> None:
        cid = await self._seed_card("noop")
        self.assertTrue(await self.store.update_card(cid, {"nope": 1}))

    async def test_update_unknown_card_is_false(self) -> None:
        self.assertFalse(
            await self.store.update_card(self.cid("nocard"), {"title": "x"}))

    async def test_list_cards_filters_by_status(self) -> None:
        a, b = self.cid("l-a"), self.cid("l-b")
        await self.store.add_card(self._card(a, status="inbox"))
        await self.store.add_card(self._card(b, status="active"))
        ids = {c.card_id for c in await self.store.list_cards(status="active", limit=500)}
        self.assertIn(b, ids)
        self.assertNotIn(a, ids)

    async def test_list_cards_orders_pinned_first(self) -> None:
        plain, pinned = self.cid("o-plain"), self.cid("o-pin")
        await self.store.add_card(self._card(plain, pinned=False))
        await self.store.add_card(self._card(pinned, pinned=True))
        ids = [c.card_id for c in await self.store.list_cards(limit=500)]
        self.assertLess(
            ids.index(pinned), ids.index(plain),
            "a pinned card sorted below an unpinned one — pinning is how the "
            "user keeps a card at the top of the board",
        )

    async def test_list_cards_counts_children(self) -> None:
        cid = await self._seed_card("counts")
        await self.store.add_note(self._note(self.cid("cnt-n"), cid))
        await self.store.add_attachment(self._attachment(self.cid("cnt-a"), cid))
        await self.store.add_objective(self._objective(self.cid("cnt-o"), cid))
        await self.store.add_objective(
            self._objective(self.cid("cnt-o2"), cid, phase="completed"))
        card = next(c for c in await self.store.list_cards(limit=500)
                    if c.card_id == cid)
        self.assertEqual(card.note_count, 1)
        self.assertEqual(card.attachment_count, 1)
        self.assertEqual(card.objective_count, 2)
        self.assertEqual(card.objective_done_count, 1)

    async def test_list_card_ids(self) -> None:
        cid = await self._seed_card("ids")
        self.assertIn(cid, await self.store.list_card_ids())

    async def test_delete_card_removes_every_child(self) -> None:
        """Cascade on Postgres, hand-written on SQLite — same outcome required."""
        cid = await self._seed_card("del")
        await self.store.add_note(self._note(self.cid("d-n"), cid))
        await self.store.add_attachment(self._attachment(self.cid("d-a"), cid))
        await self.store.add_objective(self._objective(self.cid("d-o"), cid))
        await self.store.add_event(self._event(self.cid("d-e"), cid))

        self.assertTrue(await self.store.delete_card(cid))
        self.assertIsNone(await self.store.get_card(cid))
        self.assertEqual(await self.store.list_events(cid), [])
        self.assertEqual(
            await self.child_counts(cid), (0, 0, 0, 0),
            "a child row outlived its card — on SQLite the hand-written cascade "
            "missed a table, or on Postgres a foreign key lost its ON DELETE",
        )

    async def test_delete_card_spares_other_cards(self) -> None:
        keep, drop = await self._seed_card("keep"), await self._seed_card("drop")
        await self.store.add_note(self._note(self.cid("k-n"), keep))
        await self.store.delete_card(drop)
        self.assertIsNotNone(await self.store.get_card(keep))
        self.assertEqual((await self.store.get_card(keep)).notes[0].note_id,
                         self.cid("k-n"))

    async def test_delete_unknown_card_is_false(self) -> None:
        self.assertFalse(await self.store.delete_card(self.cid("nope")))

    # -- notes --------------------------------------------------------------

    async def test_add_note_and_read_it_back(self) -> None:
        cid = await self._seed_card("n")
        self.assertTrue(await self.store.add_note(self._note(self.cid("n1"), cid)))
        card = await self.store.get_card(cid)
        self.assertEqual([n.note_id for n in card.notes], [self.cid("n1")])
        self.assertEqual(card.notes[0].body, "a note")

    async def test_add_note_to_unknown_card_is_false(self) -> None:
        """False, not an exception — Postgres would raise on the FK otherwise."""
        self.assertFalse(
            await self.store.add_note(self._note(self.cid("orph"), self.cid("nocard"))))

    async def test_add_note_bumps_the_card(self) -> None:
        """``updated_ts`` orders the board; a missed bump buries a live card."""
        cid = await self._seed_card("bump")
        before = (await self.store.get_card(cid)).updated_ts
        later = before + 100.0
        await self.store.add_note(
            self._note(self.cid("b1"), cid, created_ts=later, updated_ts=later))
        self.assertAlmostEqual(
            (await self.store.get_card(cid)).updated_ts, later, delta=1e-6)

    async def test_update_note_returns_the_new_row(self) -> None:
        cid = await self._seed_card("un")
        await self.store.add_note(self._note(self.cid("un1"), cid))
        out = await self.store.update_note(self.cid("un1"), "edited", time.time())
        self.assertIsNotNone(out)
        self.assertEqual(out.body, "edited")

    async def test_update_unknown_note_is_none(self) -> None:
        self.assertIsNone(
            await self.store.update_note(self.cid("nonote"), "x", time.time()))

    async def test_delete_note(self) -> None:
        cid = await self._seed_card("dn")
        await self.store.add_note(self._note(self.cid("dn1"), cid))
        self.assertTrue(await self.store.delete_note(self.cid("dn1")))
        self.assertEqual((await self.store.get_card(cid)).notes, [])

    async def test_delete_unknown_note_is_false(self) -> None:
        self.assertFalse(await self.store.delete_note(self.cid("nonote2")))

    # -- objectives ---------------------------------------------------------

    async def test_add_and_get_objective(self) -> None:
        cid = await self._seed_card("obj")
        self.assertTrue(
            await self.store.add_objective(self._objective(self.cid("o1"), cid)))
        got = await self.store.get_objective(self.cid("o1"))
        self.assertEqual(got.title, "a step")
        self.assertEqual(got.phase, "thinking")

    async def test_add_objective_to_unknown_card_is_false(self) -> None:
        self.assertFalse(
            await self.store.add_objective(
                self._objective(self.cid("o-orph"), self.cid("nocard"))))

    async def test_update_objective(self) -> None:
        cid = await self._seed_card("uo")
        await self.store.add_objective(self._objective(self.cid("uo1"), cid))
        out = await self.store.update_objective(
            self.cid("uo1"),
            {"phase": "completed", "outcome": "done it", "updated_ts": time.time()})
        self.assertIsNotNone(out)
        self.assertEqual(out.phase, "completed")
        self.assertEqual(out.outcome, "done it")

    async def test_update_objective_ignores_unknown_fields(self) -> None:
        cid = await self._seed_card("uow")
        await self.store.add_objective(self._objective(self.cid("uow1"), cid))
        out = await self.store.update_objective(
            self.cid("uow1"), {"title": "kept", "objective_id = 'x' --": "boom"})
        self.assertEqual(out.title, "kept")

    async def test_update_unknown_objective_is_none(self) -> None:
        self.assertIsNone(
            await self.store.update_objective(self.cid("noobj"), {"title": "x"}))

    async def test_delete_objective_returns_the_row(self) -> None:
        cid = await self._seed_card("do")
        await self.store.add_objective(self._objective(self.cid("do1"), cid))
        out = await self.store.delete_objective(self.cid("do1"))
        self.assertIsNotNone(out)
        self.assertEqual(out.objective_id, self.cid("do1"))
        self.assertIsNone(await self.store.get_objective(self.cid("do1")))

    async def test_deleting_an_objective_keeps_its_notes(self) -> None:
        """Notes outlive their objective — ``phase`` is historical context."""
        cid = await self._seed_card("don")
        await self.store.add_objective(self._objective(self.cid("don-o"), cid))
        await self.store.add_note(
            self._note(self.cid("don-n"), cid,
                       objective_id=self.cid("don-o"), phase="thinking"))
        await self.store.delete_objective(self.cid("don-o"))
        card = await self.store.get_card(cid)
        self.assertEqual(len(card.notes), 1, "a note died with its objective")
        self.assertIsNone(
            card.notes[0].objective_id,
            "the note kept a pointer to a deleted objective (expected SET NULL)")
        self.assertEqual(
            card.notes[0].phase, "thinking",
            "phase was cleared — it is history, not a live pointer")

    async def test_assign_note_to_an_objective(self) -> None:
        cid = await self._seed_card("asg")
        await self.store.add_objective(self._objective(self.cid("asg-o"), cid))
        await self.store.add_note(self._note(self.cid("asg-n"), cid))
        out = await self.store.assign_note(
            self.cid("asg-n"), self.cid("asg-o"), "doing", time.time())
        self.assertIsNotNone(out, "assign_note returned None for a live note")
        note = (await self.store.get_card(cid)).notes[0]
        self.assertEqual(note.objective_id, self.cid("asg-o"))
        self.assertEqual(note.phase, "doing")

    async def test_objectives_order_by_order_idx(self) -> None:
        cid = await self._seed_card("ord")
        await self.store.add_objective(
            self._objective(self.cid("ord-2"), cid, order_idx=2, title="second"))
        await self.store.add_objective(
            self._objective(self.cid("ord-1"), cid, order_idx=1, title="first"))
        titles = [o.title for o in (await self.store.get_card(cid)).objectives]
        self.assertEqual(titles, ["first", "second"])

    # -- attachments --------------------------------------------------------

    async def test_add_attachment_and_meta_round_trip(self) -> None:
        cid = await self._seed_card("att")
        self.assertTrue(
            await self.store.add_attachment(self._attachment(self.cid("a1"), cid)))
        att = (await self.store.get_card(cid)).attachments[0]
        self.assertEqual(att.meta, {"w": 10, "h": 20})
        self.assertEqual(att.mime, "image/png")

    async def test_add_attachment_to_unknown_card_is_false(self) -> None:
        self.assertFalse(
            await self.store.add_attachment(
                self._attachment(self.cid("a-orph"), self.cid("nocard"))))

    async def test_update_attachment(self) -> None:
        cid = await self._seed_card("ua")
        await self.store.add_attachment(self._attachment(self.cid("ua1"), cid))
        self.assertTrue(
            await self.store.update_attachment(
                self._attachment(self.cid("ua1"), cid, title="renamed",
                                 meta={"w": 99})))
        att = (await self.store.get_card(cid)).attachments[0]
        self.assertEqual(att.title, "renamed")
        self.assertEqual(att.meta, {"w": 99})

    async def test_delete_attachment_returns_the_row(self) -> None:
        cid = await self._seed_card("da")
        await self.store.add_attachment(self._attachment(self.cid("da1"), cid))
        out = await self.store.delete_attachment(self.cid("da1"))
        self.assertIsNotNone(out)
        self.assertEqual(out.attachment_id, self.cid("da1"))
        self.assertEqual((await self.store.get_card(cid)).attachments, [])

    async def test_delete_unknown_attachment_is_none(self) -> None:
        self.assertIsNone(await self.store.delete_attachment(self.cid("noatt")))

    async def test_list_all_attachments(self) -> None:
        cid = await self._seed_card("laa")
        await self.store.add_attachment(self._attachment(self.cid("laa1"), cid))
        rows = await self.store.list_all_attachments(limit=500)
        self.assertIn(self.cid("laa1"), [a.attachment_id for a in rows])

    # -- events -------------------------------------------------------------

    async def test_add_and_list_events(self) -> None:
        cid = await self._seed_card("ev")
        await self.store.add_event(self._event(self.cid("ev1"), cid))
        evs = await self.store.list_events(cid)
        self.assertEqual([e.event_id for e in evs], [self.cid("ev1")])
        self.assertEqual(evs[0].payload, {"n": 1})

    async def test_events_survive_objective_deletion(self) -> None:
        """``objective_id`` on an event is a soft pointer — history must remain."""
        cid = await self._seed_card("evo")
        await self.store.add_objective(self._objective(self.cid("evo-o"), cid))
        await self.store.add_event(
            self._event(self.cid("evo-e"), cid, objective_id=self.cid("evo-o")))
        await self.store.delete_objective(self.cid("evo-o"))
        evs = await self.store.list_events(cid)
        self.assertEqual(len(evs), 1, "an activity-timeline row died with an objective")

    async def test_list_events_limit(self) -> None:
        cid = await self._seed_card("evl")
        for i in range(3):
            await self.store.add_event(self._event(self.cid(f"evl{i}"), cid))
        self.assertLessEqual(len(await self.store.list_events(cid, limit=2)), 2)

    async def test_events_of_other_cards_are_excluded(self) -> None:
        a, b = await self._seed_card("eva"), await self._seed_card("evb")
        await self.store.add_event(self._event(self.cid("eva-1"), a))
        await self.store.add_event(self._event(self.cid("evb-1"), b))
        self.assertEqual([e.event_id for e in await self.store.list_events(a)],
                         [self.cid("eva-1")])


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedTodo(_TodoContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.todoboard.store_unified import sqlite_todo_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_todo_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def cid(self, name: str) -> str:
        return f"card-{name}"

    async def child_counts(self, card_id: str) -> tuple[int, int, int, int]:
        out = []
        async with self.store._d.reading() as conn:
            for tbl in ("todo_notes", "todo_attachments",
                        "todo_objectives", "todo_events"):
                cur = await conn.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} WHERE card_id = ?", (card_id,))
                out.append(int((await cur.fetchone())[0]))
        return tuple(out)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedTodo(_TodoContract, unittest.IsolatedAsyncioTestCase):
    """Runs against the real board database — every id is suffixed and cleaned up."""

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool
        from yuyutsava.todoboard.store_unified import pg_todo_store

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_todo_store(self.pool)

    async def asyncTearDown(self) -> None:
        # Children first, then cards. Only rows carrying this run's marker.
        like = f"%{self._suffix}"
        async with self.pool.connection() as conn:
            for tbl, col in (("todo_note_chunks", "note_id"),
                             ("todo_notes", "note_id"),
                             ("todo_attachments", "attachment_id"),
                             ("todo_objectives", "objective_id"),
                             ("todo_events", "event_id")):
                try:
                    await conn.execute(
                        f"DELETE FROM {tbl} WHERE {col} LIKE %s", (like,))  # noqa: S608
                except Exception:  # table may not exist on this deployment
                    pass
            await conn.execute("DELETE FROM todo_cards WHERE card_id LIKE %s", (like,))
        await self.pool.close()

    def cid(self, name: str) -> str:
        return f"card-{name}-{self._suffix}"

    async def child_counts(self, card_id: str) -> tuple[int, int, int, int]:
        out = []
        async with self.pool.connection() as conn:
            for tbl in ("todo_notes", "todo_attachments",
                        "todo_objectives", "todo_events"):
                cur = await conn.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} WHERE card_id = %s",  # noqa: S608
                    (card_id,))
                out.append(int((await cur.fetchone())[0]))
        return tuple(out)

    async def test_the_cascade_foreign_keys_are_present(self) -> None:
        """Postgres-only: SQLite achieves this by hand, so assert the mechanism.

        If a foreign key lost its ``ON DELETE CASCADE``, ``delete_card`` would
        either raise or orphan children — and the SQLite twin would keep passing.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) "
                "FROM pg_constraint "
                "WHERE contype='f' AND confrelid = 'todo_cards'::regclass")
            rows = await cur.fetchall()
        by_table = {t: d for t, d in rows}
        for tbl in ("todo_notes", "todo_attachments",
                    "todo_objectives", "todo_events"):
            with self.subTest(table=tbl):
                self.assertIn(tbl, by_table, f"{tbl} has no FK to todo_cards")
                self.assertIn("ON DELETE CASCADE", by_table[tbl])


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
