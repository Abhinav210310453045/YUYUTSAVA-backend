"""Resuming a session from another workspace warns, through logging, and carries on.

The warning used to be a bare ``print`` to stderr from a module the daemon also
runs, so on the daemon it bypassed the log (and its level and file) entirely
(R-LOG-01).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.conversation.sessions import resolve_session
from yuyutsava.storage.sessions import SqliteSessionStore


class ResumingElsewhere(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteSessionStore(root / "sessions.db", busy_timeout_ms=2000)
        self.first = root / "first"
        self.second = root / "second"
        self.first.mkdir()
        self.second.mkdir()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_a_different_workspace_is_logged_and_the_resume_goes_ahead(self) -> None:
        made = await self.store.create(workspace=self.first.resolve(), task="hi")
        with self.assertLogs("yuyutsava.conversation.sessions", level="WARNING") as logs:
            session, resuming = await resolve_session(
                self.store,
                workspace=self.second,
                task="(interactive)",
                resume_id=made.id,
                continue_latest=False,
            )
        self.assertTrue(resuming)
        self.assertEqual(session.id, made.id)
        self.assertIn(made.id, logs.output[0])
        self.assertIn(str(self.first.resolve()), logs.output[0])

    async def test_the_same_workspace_is_quiet(self) -> None:
        made = await self.store.create(workspace=self.first.resolve(), task="hi")
        with self.assertNoLogs("yuyutsava.conversation.sessions", level="WARNING"):
            await resolve_session(
                self.store,
                workspace=self.first,
                task="(interactive)",
                resume_id=made.id,
                continue_latest=False,
            )


if __name__ == "__main__":
    unittest.main()
