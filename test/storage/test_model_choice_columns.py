"""A conversation's chosen models, and the provider behind each call, survive a round trip.

``sessions.model_ref`` / ``subagent_models`` hold what ``/model`` and the app's
model picker choose for one conversation; ``llm_usage.provider`` says who served
a call, which a model id alone cannot (the same Claude runs on Anthropic, Vertex
and Bedrock). Both backends, and the in-place upgrade of existing SQLite files —
someone upgrading has months of sessions and usage that must not be rebuilt away.

Postgres coverage is skipped (not failed) when no server is reachable.

Run:  .venv/bin/python -m pytest test/storage/test_model_choice_columns.py
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from test.support.pg import pg_dsn, requires_pg
from yuyutsava.storage.ids import mint_thread_id
from yuyutsava.storage.models import ALL_SUBAGENTS, ModelChoice
from yuyutsava.storage.sessions import SessionNotFound, SqliteSessionStore
from yuyutsava.storage.usage import SqliteUsageStore, UsageRow

CHOICE = ModelChoice(
    model="sonnet",
    subagents=((ALL_SUBAGENTS, "groq/llama-3.3-70b-versatile"), ("researcher", "gemini-flash")),
)


class TheChoice(unittest.TestCase):
    def test_a_named_subagent_wins_over_all(self) -> None:
        self.assertEqual(CHOICE.for_subagent("researcher"), "gemini-flash")
        self.assertEqual(CHOICE.for_subagent("writer"), "groq/llama-3.3-70b-versatile")

    def test_nothing_chosen_follows_the_task(self) -> None:
        self.assertEqual(ModelChoice().for_subagent("writer"), "")


class _SessionChoiceContract:
    store: object
    workspace: Path

    async def _session(self):
        return await self.store.create(
            workspace=self.workspace, task="t", thread_id=f"mc_test_{mint_thread_id('cli')}"
        )

    async def test_a_new_session_follows_models_toml(self) -> None:
        session = await self._session()
        self.assertEqual((await self.store.get(session.id)).models, ModelChoice())

    async def test_a_choice_round_trips_through_get_and_list(self) -> None:
        session = await self._session()
        await self.store.set_model_choice(session.id, CHOICE)
        self.assertEqual((await self.store.get(session.id)).models, CHOICE)
        listed = [s for s in await self.store.list(limit=500) if s.id == session.id]
        self.assertEqual(listed[0].models, CHOICE)

    async def test_clearing_it_follows_models_toml_again(self) -> None:
        session = await self._session()
        await self.store.set_model_choice(session.id, CHOICE)
        await self.store.set_model_choice(session.id, ModelChoice())
        self.assertEqual((await self.store.get(session.id)).models, ModelChoice())

    async def test_a_session_that_does_not_exist_is_an_error(self) -> None:
        with self.assertRaises(SessionNotFound):
            await self.store.set_model_choice("mc_test_nobody", CHOICE)


class SqliteSessionChoice(_SessionChoiceContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteSessionStore(Path(self._tmp.name) / "sessions.db")
        self.workspace = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_a_v3_file_is_upgraded_in_place(self) -> None:
        path = Path(self._tmp.name) / "legacy.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sessions_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, workspace TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                memory_files_count INTEGER NOT NULL DEFAULT 0,
                db_row_bytes INTEGER NOT NULL DEFAULT 0,
                task_preview TEXT NOT NULL DEFAULT '',
                schema_version INTEGER NOT NULL DEFAULT 1,
                origin TEXT NOT NULL DEFAULT 'cli', title TEXT NOT NULL DEFAULT ''
            );
        """)
        con.execute("INSERT INTO sessions_meta VALUES ('schema_version', '3')")
        con.execute(
            "INSERT INTO sessions (id, thread_id, workspace, status, created_at, updated_at, title)"
            " VALUES ('old', 'old', '/w', 'idle', 1.0, 1.0, 'kept')"
        )
        con.commit()
        con.close()

        store = SqliteSessionStore(path)
        old = await store.get("old")
        self.assertEqual((old.title, old.models), ("kept", ModelChoice()))
        await store.set_model_choice("old", CHOICE)
        self.assertEqual((await store.get("old")).models, CHOICE)


@requires_pg
class PgSessionChoice(_SessionChoiceContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg import migrations
        from yuyutsava.storage.pg.pool import PgPool
        from yuyutsava.storage.sessions.pg_impl import PgSessionStore

        settings = StorageSettings(backend="postgres", pg_dsn=pg_dsn())
        self.pool = PgPool(settings)
        await self.pool.open()
        await migrations.apply(self.pool)
        self.store = PgSessionStore(settings, pool=self.pool)
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE id LIKE 'mc_test_%%'")
        await self.pool.close()
        self._tmp.cleanup()


def _usage(rid: str, provider: str = "vertex") -> UsageRow:
    return UsageRow(
        id=rid,
        ts=time.time(),
        thread_id="",
        task_id="",
        role="cli",
        model="gemini-3.5-flash",
        input_tokens=10,
        output_tokens=1,
        est_cost_usd=0.0,
        provider=provider,
    )


class _UsageProviderContract:
    async def test_the_provider_round_trips(self) -> None:
        await self.store.add(_usage("usg_mc_1"))
        got = [r for r in await self.store.list(limit=50) if r.id == "usg_mc_1"]
        self.assertEqual(got[0].provider, "vertex")

    async def test_a_row_without_one_reads_back_empty(self) -> None:
        await self.store.add(_usage("usg_mc_2", provider=""))
        got = [r for r in await self.store.list(limit=50) if r.id == "usg_mc_2"]
        self.assertEqual(got[0].provider, "")


class SqliteUsageProvider(_UsageProviderContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_a_v2_table_is_upgraded_in_place(self) -> None:
        path = Path(self._tmp.name) / "legacy.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE llm_usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE llm_usage (
                id TEXT PRIMARY KEY, ts REAL NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                est_cost_usd REAL NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0
            );
        """)
        con.execute("INSERT INTO llm_usage_meta VALUES ('schema_version', '2')")
        con.execute(
            "INSERT INTO llm_usage VALUES ('usg_old', 1.0, '', '', 'cli', 'm', 5, 1, 0.0, 0, 0)"
        )
        con.commit()
        con.close()

        store = SqliteUsageStore(path)
        old = [r for r in await store.list(limit=50) if r.id == "usg_old"]
        self.assertEqual((old[0].input_tokens, old[0].provider), (5, ""))
        await store.add(_usage("usg_mc_new"))
        new = [r for r in await store.list(limit=50) if r.id == "usg_mc_new"]
        self.assertEqual(new[0].provider, "vertex")


@requires_pg
class PgUsageProvider(_UsageProviderContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg import migrations
        from yuyutsava.storage.pg.pool import PgPool
        from yuyutsava.storage.usage import PgUsageStore

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=pg_dsn()))
        await self.pool.open()
        await migrations.apply(self.pool)
        self.store = PgUsageStore(self.pool)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM llm_usage WHERE id LIKE 'usg_mc_%%'")
        await self.pool.close()


if __name__ == "__main__":
    unittest.main()
