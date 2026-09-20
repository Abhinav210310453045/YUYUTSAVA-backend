"""Characterization: the whole `TodoExchange` surface, against a fake store.

`R-SOL-01`: the exchange is 646 lines and 30 methods, and it is the *only* write
path to the board — every router, tool and agent goes through it. Nothing tested
those 30 methods, so a split had nothing to be checked against.

Driven entirely through :class:`~test.todoboard.fake_store.FakeTodoStore`: the
real board holds real work and must not be touched by a test run.

What is pinned here is not just "each method returns something" but the parts a
split would plausibly break — the validation vocabularies, which mutations
append to the event trail, the typed exception each failure raises, and the
workspace-before-row ordering that `add_card` rolls back.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from test.todoboard.fake_store import FakeTodoStore
from yuyutsava.todoboard.exchange import (
    TodoExchange,
    TodoNotFoundError,
    TodoStorageError,
    TodoValidationError,
    board_workspace_root,
)

_REAL_STATE_DIR = pathlib.Path.home() / ".yuyutsava"


class BoardOffDisk(unittest.IsolatedAsyncioTestCase):
    """Base case: a fake store, and a state dir that is not the user's.

    `add_card` creates a card's workspace directory on disk *before* it writes
    the row, so an unredirected run leaves real directories in the user's board.
    That is not hypothetical — an earlier version of this file set
    ``YUYUTSAVA_STATE_DIR``, which nothing reads, and quietly created 75
    directories under ``~/.yuyutsava/blobs/todoboard``.

    The redirect is per-test rather than at import: ``YUYUTSAVA_HOME`` is
    process-wide, and setting it at import time moved the paths baked into the
    agent prompt and broke the fingerprint snapshot in the same run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="yuyutsava-test-home-")
        self._env = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": self._tmp.name})
        self._env.start()
        root = board_workspace_root()
        if root == _REAL_STATE_DIR or _REAL_STATE_DIR in root.parents:
            self._env.stop()
            self._tmp.cleanup()
            raise RuntimeError(
                f"refusing to run: the board workspace root is {root}, inside the "
                f"real state dir {_REAL_STATE_DIR}. These tests create card "
                "workspaces on disk and must never touch the user's board."
            )
        self.store = FakeTodoStore()
        self.ex = TodoExchange(self.store)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    async def _card(self, title: str = "a card", **kw):
        return await self.ex.add_card(title, **kw)


class Cards(BoardOffDisk):
    async def test_a_card_is_created_with_a_workspace(self) -> None:
        card = await self._card("write the thing", tags=["docs"])
        self.assertTrue(card.card_id.startswith("tdo_"))
        self.assertEqual(card.title, "write the thing")
        self.assertEqual(card.status, "inbox")
        self.assertEqual(card.tags, ["docs"])
        # The workspace has to exist before any tr_* write targets it.
        self.assertTrue(os.path.isdir(card.workspace_path))

    async def test_a_first_note_can_be_created_with_the_card(self) -> None:
        """`add_card` writes into the notes domain — the cross-domain step."""
        card = await self._card(note="the first thought", note_author="user")
        self.assertEqual(len(card.notes), 1)
        self.assertEqual(card.notes[0].body, "the first thought")

    async def test_a_card_round_trips(self) -> None:
        card = await self._card()
        got = await self.ex.get_card(card.card_id)
        self.assertEqual(got.card_id, card.card_id)

    async def test_an_unknown_card_is_not_found(self) -> None:
        with self.assertRaises(TodoNotFoundError):
            await self.ex.get_card("tdo_nope")

    async def test_a_status_change_is_recorded_on_the_timeline(self) -> None:
        card = await self._card()
        await self.ex.update_card(card.card_id, status="active")
        self.assertIn("card_status", self.store.event_kinds(card.card_id))

    async def test_deleting_a_card_takes_its_children(self) -> None:
        """The deletes return None by contract, so assert the effect."""
        card = await self._card(note="gone too")
        await self.ex.delete_card(card.card_id)
        self.assertEqual(self.store.cards, {})
        self.assertEqual(self.store.notes, {}, "notes must go with their card")

    async def test_the_board_can_be_queried_and_listed(self) -> None:
        await self._card("one")
        await self._card("two")
        self.assertEqual(len(await self.ex.query_board()), 2)
        self.assertEqual(len(await self.ex.list_card_ids()), 2)
        snapshot = await self.ex.board_snapshot()
        self.assertEqual(len(snapshot.cards), 2)


class Objectives(BoardOffDisk):
    async def test_an_objective_is_created_and_recorded(self) -> None:
        card = await self._card()
        obj = await self.ex.add_objective(card.card_id, "ship it")
        self.assertEqual(obj.title, "ship it")
        self.assertIn("objective_created", self.store.event_kinds(card.card_id))

    async def test_an_objective_needs_its_card_to_exist(self) -> None:
        with self.assertRaises(TodoNotFoundError):
            await self.ex.add_objective("tdo_nope", "ship it")

    async def test_updating_an_objective_records_what_changed(self) -> None:
        card = await self._card()
        obj = await self.ex.add_objective(card.card_id, "ship it")
        await self.ex.update_objective(obj.objective_id, title="ship it well")
        kinds = self.store.event_kinds(card.card_id)
        self.assertTrue({"objective_updated", "objective_phase"} & set(kinds))

    async def test_deleting_an_objective_is_recorded(self) -> None:
        card = await self._card()
        obj = await self.ex.add_objective(card.card_id, "ship it")
        await self.ex.delete_objective(obj.objective_id)
        self.assertIn("objective_deleted", self.store.event_kinds(card.card_id))


class Notes(BoardOffDisk):
    async def test_a_plain_note_writes_no_event(self) -> None:
        """Only an *assigned* note is timeline-worthy; a bare note is not."""
        card = await self._card()
        before = len(self.store.events)
        await self.ex.add_note(card.card_id, "just a thought")
        self.assertEqual(len(self.store.events), before)

    async def test_a_note_assigned_to_an_objective_is_recorded(self) -> None:
        card = await self._card()
        obj = await self.ex.add_objective(card.card_id, "ship it")
        await self.ex.add_note(
            card.card_id, "towards it", objective_id=obj.objective_id, phase="doing"
        )
        self.assertIn("note_assigned", self.store.event_kinds(card.card_id))

    async def test_a_note_can_be_reassigned_updated_and_deleted(self) -> None:
        card = await self._card()
        note = await self.ex.add_note(card.card_id, "first")
        obj = await self.ex.add_objective(card.card_id, "ship it")

        moved = await self.ex.assign_note(note.note_id, objective_id=obj.objective_id)
        self.assertEqual(moved.objective_id, obj.objective_id)

        edited = await self.ex.update_note(note.note_id, "second")
        self.assertEqual(edited.body, "second")

        await self.ex.delete_note(note.note_id)
        self.assertEqual(self.store.notes, {})

    async def test_the_event_list_is_readable(self) -> None:
        card = await self._card()
        await self.ex.update_card(card.card_id, status="active")
        self.assertTrue(await self.ex.list_events(card.card_id))


class Attachments(BoardOffDisk):
    async def _file(self) -> str:
        """A real file: a `file` attachment is checked against the filesystem,
        which is the point — a card must not reference something unreadable."""
        path = os.path.join(tempfile.mkdtemp(), "a.txt")
        with open(path, "w") as fh:
            fh.write("contents")
        return path

    async def test_a_file_attachment_must_name_a_real_file(self) -> None:
        card = await self._card()
        with self.assertRaises(TodoValidationError):
            await self.ex.attach(card.card_id, kind="file", title="a.txt")

    async def test_attaching_records_the_artifact(self) -> None:
        card = await self._card()
        att = await self.ex.attach(
            card.card_id, kind="file", path=await self._file(), title="a.txt"
        )
        self.assertEqual(att.kind, "file")
        self.assertIn("artifact_attached", self.store.event_kinds(card.card_id))

    async def test_an_attachment_needs_a_known_kind(self) -> None:
        card = await self._card()
        with self.assertRaises(TodoValidationError):
            await self.ex.attach(card.card_id, kind="not-a-kind", title="x")

    async def test_attachments_can_be_listed_and_deleted(self) -> None:
        card = await self._card()
        att = await self.ex.attach(
            card.card_id, kind="file", path=await self._file(), title="a.txt"
        )
        self.assertTrue(await self.ex.list_all_attachments())
        await self.ex.delete_attachment(att.attachment_id)
        self.assertEqual(self.store.attachments, {})


class Validation(BoardOffDisk):
    """The vocabularies are the exchange's whole reason for existing: bad data
    must be refused before the store sees it."""

    async def test_a_card_needs_a_title(self) -> None:
        with self.assertRaises(TodoValidationError):
            await self.ex.add_card("   ")

    async def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaises(TodoValidationError):
            await self.ex.add_card("x", status="not-a-status")

    async def test_an_unknown_phase_is_refused(self) -> None:
        card = await self._card()
        with self.assertRaises(TodoValidationError):
            await self.ex.add_objective(card.card_id, "ship it", phase="not-a-phase")

    async def test_nothing_invalid_reaches_the_store(self) -> None:
        with self.assertRaises(TodoValidationError):
            await self.ex.add_card("x", status="not-a-status")
        self.assertEqual(self.store.cards, {})


class StorageFaults(BoardOffDisk):
    """A backend hiccup must surface as `TodoStorageError`, never as whatever
    the driver raised, and must not leave debris behind."""

    async def test_a_failing_insert_becomes_a_storage_error(self) -> None:
        ex = TodoExchange(FakeTodoStore(fail_on="add_card"))
        with self.assertRaises(TodoStorageError):
            await ex.add_card("doomed")

    async def test_a_failing_insert_removes_the_workspace_it_made(self) -> None:
        """Workspace dir first, row second — so a failed row must roll the
        directory back, or the board grows orphans nobody can reach."""
        store = FakeTodoStore(fail_on="add_card")
        ex = TodoExchange(store)
        with self.assertRaises(TodoStorageError):
            await ex.add_card("doomed")
        self.assertEqual(store.cards, {})

    async def test_a_failing_event_write_does_not_fail_the_mutation(self) -> None:
        """History is an enhancement, never a gate."""
        store = FakeTodoStore(fail_on="add_event")
        ex = TodoExchange(store)
        card = await ex.add_card("survives")
        await ex.update_card(card.card_id, status="active")
        self.assertEqual(store.cards[card.card_id].status, "active")


if __name__ == "__main__":
    unittest.main()
