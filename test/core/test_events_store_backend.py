"""A process without the daemon reads and writes the daemon's preferences (R-BUG-21).

On the Postgres backend the daemon keeps ``user_prefs`` in Postgres. The CLI's
``prefs`` command and the REPL's ``/voice`` toggles built a bare ``Store()`` —
SQLite-only — so a preference set in the terminal landed in ``state.db`` and
the daemon never saw it; a standalone CLI session injected no preferences at
all; and a CLI purge cleared pending asks from a file they were never written
to. ``open_events_store`` opens the configured backend's store, and
``AppContext.events`` routes to Postgres when it has a pool.

Run:  .venv/bin/python -m pytest test/core/test_events_store_backend.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test.support.pg import pg_dsn, requires_pg
from yuyutsava.cli.commands import chat as chat_cmd
from yuyutsava.core.app_context import AppContext, open_events_store
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.routing.health import StorageHealth


class _ScratchHome(unittest.IsolatedAsyncioTestCase):
    backend = "sqlite"

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = {"YUYUTSAVA_HOME": self._tmp.name, "YUYUTSAVA_STORAGE_BACKEND": self.backend}
        if self.backend == "postgres":
            env["YUYUTSAVA_PG_DSN"] = pg_dsn()
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.key = f"test.r_bug_21.{uuid.uuid4().hex[:8]}"

    async def asyncTearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    async def _sqlite_value(self):
        store = Store()  # the state.db twin, read directly
        await store.start()
        try:
            return await PrefsStore(store).get(self.key)
        finally:
            await store.stop()


class OnSqlite(_ScratchHome):
    async def test_it_is_the_state_db(self) -> None:
        async with open_events_store() as store:
            await PrefsStore(store).set(self.key, "v")
        self.assertEqual(await self._sqlite_value(), "v")


@requires_pg
class OnPostgres(_ScratchHome):
    backend = "postgres"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings = StorageSettings.from_env()
        self.pool = PgPool(self.settings)
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM user_prefs WHERE key = %s", (self.key,))
        await self.pool.close()
        await super().asyncTearDown()

    async def _daemon_value(self):
        """What the daemon reads: the store built the way bootstrap builds it."""
        store = Store.for_backend(self.settings, pg_pool=self.pool, health=StorageHealth(self.pool))
        await store.start()
        try:
            return await PrefsStore(store).get(self.key)
        finally:
            await store.stop()

    async def test_a_cli_write_is_the_daemons_row(self) -> None:
        async with open_events_store() as store:
            await PrefsStore(store).set(self.key, "from the terminal")
        self.assertEqual(await self._daemon_value(), "from the terminal")
        self.assertIsNone(await self._sqlite_value(), "the write went to state.db")

    async def test_the_app_context_routes_to_postgres_too(self) -> None:
        ctx = AppContext(session_store=None, visual_store=None, feedback_store=None)
        store, owned = ctx.events(self.settings, pg_pool=self.pool)
        self.assertTrue(owned)
        await store.start()
        try:
            await PrefsStore(store).set(self.key, "via the context")
        finally:
            await store.stop()
        self.assertEqual(await self._daemon_value(), "via the context")


class TheCliStackGetsPreferences(_ScratchHome):
    async def test_a_one_shot_turn_wires_a_prefs_store(self) -> None:
        seen = {}

        class _Stop(Exception):
            pass

        async def capture(*_a, **kw):
            seen.update(kw)
            raise _Stop

        opts = mock.MagicMock(workspace=Path(self._tmp.name))
        with mock.patch.object(chat_cmd, "build_agent_stack", capture):
            with self.assertRaises(_Stop):
                await chat_cmd.run_chat(opts, task="hi", docker_pull_paths="")
        wiring = seen.get("wiring")
        self.assertIsNotNone(wiring, "the stack was built without wiring")
        self.assertIsInstance(wiring.prefs_store, PrefsStore)


if __name__ == "__main__":
    unittest.main()
