"""``build_storage`` is constructible without a daemon.

Phase 3 step 3.3 (ADR-003). ``build_daemon`` was one 927-line function with 58
branches — the DI container, migration runner, feature-flag evaluator, backend
selector, agent factory and lifecycle owner at once. Its defect was not length:
it was that **the knowledge of how the system fits together existed only as
statement order inside one body**, which no tool can verify and no reader can
hold.

The concrete consequence was untestability. Exercising twenty lines of storage
wiring meant constructing an entire daemon — a Postgres pool, a LangGraph host,
an agent graph — so nobody did.

That is what this test demonstrates is fixed: the storage subsystem builds, on
either backend, in milliseconds, with nothing else running.

Postgres cases skip when no server is reachable.

Run:  .venv/bin/python test/daemon/test_build_storage.py
"""

from __future__ import annotations

import os
import socket
import tempfile
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

#: Every field the rest of build_daemon reads off the record. A missing one
#: means the extraction dropped a local that 600 lines downstream still expect.
REQUIRED_FIELDS = (
    "settings", "pg_pool", "storage_health", "stores", "embedder", "mem_settings",
    "artifact_store", "summary_store", "transcript_store", "voice_store",
    "memory_store", "usage_store", "task_registry", "events", "model_router",
    "fallback_reason",
)


def _opts():
    from yuyutsava.daemon.bootstrap import DaemonOptions

    return DaemonOptions(workspace=Path.cwd(), headless=True, voice=False, verbose=False)


class BuildStorageStandalone(unittest.IsolatedAsyncioTestCase):
    """The point of the extraction: no daemon required."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("YUYUTSAVA_STATE_DIR", "YUYUTSAVA_STORAGE_BACKEND")}
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        os.environ["YUYUTSAVA_STORAGE_BACKEND"] = "sqlite"

    async def asyncTearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_builds_on_sqlite_with_no_daemon(self) -> None:
        from yuyutsava.daemon.bootstrap import build_storage

        st = await build_storage(_opts())
        try:
            self.assertEqual(st.settings.backend, "sqlite")
            self.assertIsNone(st.pg_pool)
            self.assertIsNone(st.storage_health, "no health probe without Postgres")
            self.assertIsNone(st.fallback_reason, "SQLite mode is not a fallback")
        finally:
            await st.events.stop()

    async def test_record_carries_every_field_build_daemon_reads(self) -> None:
        from yuyutsava.daemon.bootstrap import build_storage

        st = await build_storage(_opts())
        try:
            missing = [f for f in REQUIRED_FIELDS if not hasattr(st, f)]
            self.assertEqual(
                missing, [],
                f"StorageSubsystem is missing {missing}. build_daemon unpacks "
                f"these into locals that later sections read, so a dropped field "
                f"is a NameError several hundred lines away.",
            )
        finally:
            await st.events.stop()

    async def test_events_store_is_started(self) -> None:
        """``build_daemon`` relied on the inline block having called start()."""
        from yuyutsava.daemon.bootstrap import build_storage

        st = await build_storage(_opts())
        try:
            # A started backend answers queries; an unstarted one raises
            # "SqliteEventsBackend.open() must be called first".
            await st.events.list_consent_rules()
        finally:
            await st.events.stop()


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class BuildStorageOnPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("YUYUTSAVA_STATE_DIR", "YUYUTSAVA_STORAGE_BACKEND")}
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        os.environ["YUYUTSAVA_STORAGE_BACKEND"] = "postgres"

    async def asyncTearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_builds_postgres_stores_and_health(self) -> None:
        from yuyutsava.daemon.bootstrap import build_storage

        st = await build_storage(_opts())
        try:
            self.assertEqual(st.settings.backend, "postgres")
            self.assertIsNotNone(st.pg_pool)
            self.assertIsNotNone(st.storage_health, "spillover needs a health probe")
            self.assertIsNone(st.fallback_reason, "Postgres was reachable")
            # Was a class-name check; the store is UnifiedArtifactStore on both
            # backends since ADR-002 step 2.5b, so assert the capability the
            # name used to stand for.
            self.assertTrue(
                st.artifact_store.supports_recall,
                "Postgres + embedder produced an artifact store without recall; "
                "ctx_recall would silently return nothing",
            )
        finally:
            await st.events.stop()
            if st.pg_pool is not None:
                await st.pg_pool.close()

    async def test_embedder_is_built_when_postgres_is_live(self) -> None:
        """pgvector features depend on this; it was an inline conditional."""
        from yuyutsava.daemon.bootstrap import build_storage

        st = await build_storage(_opts())
        try:
            self.assertIsNotNone(st.embedder)
        finally:
            await st.events.stop()
            if st.pg_pool is not None:
                await st.pg_pool.close()


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
