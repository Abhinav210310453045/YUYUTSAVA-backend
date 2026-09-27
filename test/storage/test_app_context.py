"""``purge_session`` gets its stores handed over; nothing is looked up.

ADR-003, findings ``F-S08`` and ``R-GLB-01``.

``purge_session`` touches the sessions, visuals, feedback and events stores. They
were process globals, reached through ``get_default_*`` — which hid the blast
radius, made tests set and restore globals (these tests failed whenever another
test in the process had cached one first), and in a CLI process on Postgres
resolved feedback to the SQLite twin, so a CLI delete left the session's
feedback rows in Postgres (``R-BUG-19``). Now every caller supplies an
``AppContext``: the daemon from its own stores, a CLI command through
``open_app_context``.

Run:  .venv/bin/python -m pytest test/storage/test_app_context.py
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.core.app_context import AppContext, open_app_context
from yuyutsava.core.purge import purge_session
from yuyutsava.storage.sessions import SqliteSessionStore


class _Recorder:
    """Records the threads it was asked to purge."""

    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    async def delete_for_thread(self, thread_id: str) -> int:
        self.deleted_threads.append(thread_id)
        return 1


class TheContextIsExplicit(unittest.TestCase):
    def test_the_stores_are_required(self) -> None:
        with self.assertRaises(TypeError):
            AppContext()  # type: ignore[call-arg]

    def test_it_is_frozen(self) -> None:
        ctx = AppContext(session_store=object(), visual_store=object(), feedback_store=object())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.visual_store = object()  # type: ignore[misc]

    def test_purge_session_requires_one(self) -> None:
        param = inspect.signature(purge_session).parameters["ctx"]
        self.assertIs(param.default, inspect.Parameter.empty)

    def test_no_process_global_accessor_is_left(self) -> None:
        """They cannot quietly come back: a module that grew one fails here."""
        for module, names in (
            ("yuyutsava.storage.sessions", ("get_default_session_store",)),
            ("yuyutsava.visuals.store", ("get_default_visual_store", "set_default_visual_store")),
            (
                "yuyutsava.storage.feedback_store",
                ("get_default_feedback_store", "set_default_feedback_store"),
            ),
            ("yuyutsava.todoboard.store", ("get_default_todo_store",)),
            ("yuyutsava.todoboard.exchange", ("get_default_exchange",)),
            ("yuyutsava.todoboard.recall", ("get_default_note_index",)),
            ("yuyutsava.core.app_context", ("GLOBAL_CONTEXT",)),
        ):
            mod = importlib.import_module(module)
            for name in names:
                with self.subTest(module=module, name=name):
                    self.assertFalse(hasattr(mod, name))


class _ScratchHome(unittest.IsolatedAsyncioTestCase):
    """A scratch state dir on SQLite: the purge sweeps the checkpointer and state
    DB for the thread, and must touch no real file."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"YUYUTSAVA_HOME": self._tmp.name, "YUYUTSAVA_STORAGE_BACKEND": "sqlite"}
        )
        self._env.start()

    async def asyncTearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class APurgeUsesWhatItIsGiven(_ScratchHome):
    async def test_every_store_in_the_context_is_used(self) -> None:
        store = SqliteSessionStore(Path(self._tmp.name) / "sessions.db")
        session = await store.create(workspace=Path(self._tmp.name), origin="cli", task="t")
        vis, fb = _Recorder(), _Recorder()

        report = await purge_session(
            session.id,
            ctx=AppContext(session_store=store, visual_store=vis, feedback_store=fb),
        )

        self.assertEqual(vis.deleted_threads, [session.thread_id])
        self.assertEqual(fb.deleted_threads, [session.thread_id])
        self.assertTrue(report.session_row_deleted)
        self.assertTrue(report.checkpoints_deleted)


class ACommandOpensTheBackendsOwnStores(_ScratchHome):
    async def test_on_sqlite_the_stores_are_the_state_dir_twins(self) -> None:
        async with open_app_context() as ctx:
            self.assertIsNone(ctx.pg_pool)
            session = await ctx.session_store.create(
                workspace=Path(self._tmp.name), origin="cli", task="t"
            )
            await ctx.feedback_store.upsert(
                thread_id=session.thread_id,
                session_id=session.id,
                message_ref="m1",
                rating="up",
                user_text="u",
                assistant_text="a",
            )
            report = await purge_session(session.id, ctx=ctx)
            remaining = await ctx.feedback_store.list_for_thread(session.thread_id)
        self.assertEqual(report.rows.get("message_feedback"), 1)
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
