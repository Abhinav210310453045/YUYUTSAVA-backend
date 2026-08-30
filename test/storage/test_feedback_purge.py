"""Session deletion must remove message feedback.

Regression test for a data-retention bug found by the Phase 2 twin-conformance
work (2026-08-08).

``message_feedback`` rows store ``user_text`` and ``assistant_text`` verbatim —
the actual conversation. The table sits outside the thread-hub FK graph, so
neither ``_STATE_TABLES``/``_PG_CHILD_TABLES`` nor the Postgres cascade reached
it, and ``FeedbackStore`` had no ``delete_for_thread`` at all. Deleting a session
therefore left the user's message content on disk.

This is the failure mode ADR-002 predicts from hardcoded purge lists: adding a
domain means remembering to edit a list in an unrelated module, and forgetting is
silent.

Run:  .venv/bin/python test/storage/test_feedback_purge.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from yuyutsava.storage.feedback_store_unified import sqlite_feedback_store


class FeedbackDeleteForThread(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_feedback_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _add(self, thread_id: str, ref: str) -> None:
        await self.store.upsert(
            thread_id=thread_id,
            session_id=thread_id,
            message_ref=ref,
            rating="up",
            user_text="SECRET user question",
            assistant_text="SECRET assistant answer",
        )

    async def test_deletes_only_the_target_thread(self) -> None:
        await self._add("thread-a", "m1")
        await self._add("thread-a", "m2")
        await self._add("thread-b", "m1")

        deleted = await self.store.delete_for_thread("thread-a")
        self.assertEqual(deleted, 2)

        self.assertEqual(await self.store.list_for_thread("thread-a"), [])
        survivors = await self.store.list_for_thread("thread-b")
        self.assertEqual(len(survivors), 1, "an unrelated thread's feedback was destroyed")

    async def test_no_conversation_text_survives(self) -> None:
        """The point of the fix: verbatim message content must be gone."""
        await self._add("thread-a", "m1")
        await self.store.delete_for_thread("thread-a")

        remaining = await self.store.list_all()
        leaked = [
            r for r in remaining
            if "SECRET" in (r.user_text or "") or "SECRET" in (r.assistant_text or "")
        ]
        self.assertEqual(
            leaked, [],
            "conversation text survived session deletion — the retention bug is back",
        )

    async def test_unknown_thread_is_a_no_op(self) -> None:
        self.assertEqual(await self.store.delete_for_thread("never-existed"), 0)


class PurgeWiring(unittest.IsolatedAsyncioTestCase):
    """purge_session must actually delete feedback — asserted by running it.

    This was a source-grep for ``get_default_feedback_store`` while that call was
    the only way to observe the wiring. Phase 3.4 gave ``purge_session`` an
    explicit ``AppContext``, so the check can now be what it always wanted to be:
    run a real purge against a real session and watch the feedback store get
    called. Strictly stronger — a grep passes on a call sitting in dead code.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = {
            k: os.environ.get(k)
            for k in ("YUYUTSAVA_STATE_DIR", "YUYUTSAVA_STORAGE_BACKEND")
        }
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        os.environ["YUYUTSAVA_STORAGE_BACKEND"] = "sqlite"

    async def asyncTearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_purge_session_deletes_feedback(self) -> None:
        from yuyutsava.storage.context import AppContext
        from yuyutsava.storage.purge import purge_session
        from yuyutsava.storage.sessions import get_default_session_store

        seen: list[str] = []

        class RecordingFeedback:
            async def delete_for_thread(self, thread_id: str) -> int:
                seen.append(thread_id)
                return 3

        class NoopVisuals:
            async def delete_for_thread(self, thread_id: str) -> int:
                return 0

        store = get_default_session_store()
        session = await store.create(workspace=Path(self._tmp.name), origin="cli", task="t")

        report = await purge_session(
            session.id,
            ctx=AppContext(
                session_store=store,
                feedback_store=RecordingFeedback(),
                visual_store=NoopVisuals(),
            ),
        )

        self.assertEqual(
            seen, [session.thread_id],
            "purge_session did not ask the feedback store to delete this "
            "thread. Session deletion would again leave verbatim "
            "user/assistant text on disk.",
        )
        self.assertEqual(report.rows.get("message_feedback"), 3)
        self.assertTrue(report.session_row_deleted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
