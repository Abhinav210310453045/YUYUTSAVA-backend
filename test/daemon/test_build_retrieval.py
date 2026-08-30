"""``build_retrieval`` — the Postgres-only branch, finally under test.

This suite exists because of finding Y. The TODO-note recall index is built
inside::

    if pg_pool is not None and embedder is not None:
        from yuyutsava.todoboard.recall import TodoNoteIndex, set_default_note_index
        ...
        await note_index.sync(get_default_exchange())

That branch **only runs on Postgres**, and while it lived inside
``build_daemon`` nothing could reach it: ``build_daemon`` starts uvicorn, the
LangGraph host and MCP servers, so it never returns in a test. A ``NameError``
sat in it undetected while every SQLite suite, all framework contracts and a
353-module import sweep stayed green.

Extracting the block is what makes it testable. These are the tests that would
have caught that bug by *executing* the code rather than by static analysis.

Both backends are covered on purpose: the SQLite case asserts the index is
correctly **absent** (so the exchange's embed-on-write hooks no-op), which is
just as much a behaviour as building it.

Run:  .venv/bin/python test/daemon/test_build_retrieval.py
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN, StorageSettings


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


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("YUYUTSAVA_STATE_DIR",)}
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        self.home = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class RetrievalOnSqlite(_Base):
    """No pgvector: skills fall back to the keyword twin, no note index."""

    async def test_builds_without_postgres(self) -> None:
        from yuyutsava.daemon.bootstrap import build_retrieval
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.storage.factory import StoreFactory

        stores = StoreFactory(StorageSettings(backend="sqlite"))
        rtv = await build_retrieval(
            home=self.home, stores=stores,
            mem_settings=MemorySettings.from_env(default_enabled=False),
            pg_pool=None, embedder=None,
        )
        self.assertIsNotNone(rtv.skill_registry)
        # Was a class-name check; the store is UnifiedSkillStore on both
        # backends since ADR-002 step 2.5b, so assert the behaviour the name
        # used to stand for.
        self.assertFalse(
            rtv.skill_store.supports_semantic_search,
            "the skill store claims semantic search without pgvector — every "
            "lookup would try to embed and fall back on each call",
        )
        self.assertIsNone(
            rtv.note_index,
            "a note index was built without pgvector; the exchange's "
            "embed-on-write hooks expect None here and would try to embed",
        )

    async def test_skill_registry_sees_bundled_skills(self) -> None:
        from yuyutsava.daemon.bootstrap import build_retrieval
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.storage.factory import StoreFactory

        rtv = await build_retrieval(
            home=self.home, stores=StoreFactory(StorageSettings(backend="sqlite")),
            mem_settings=MemorySettings.from_env(default_enabled=False),
            pg_pool=None, embedder=None,
        )
        bundled = [s for s in rtv.skill_registry.scan() if s.scope == "bundled"]
        self.assertGreater(len(bundled), 0, "no bundled skills discovered")


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class RetrievalOnPostgres(_Base):
    """**The branch finding Y hid in.** Executes it, rather than analysing it."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.storage.pg.pool import PgPool

        self.settings = StorageSettings(backend="postgres", pg_dsn=_pg_dsn())
        self.pool = PgPool(self.settings)
        await self.pool.open()
        self.mem = MemorySettings.from_env(default_enabled=True)
        self.embedder = Embedder(self.mem)

    async def asyncTearDown(self) -> None:
        await self.pool.close()
        await super().asyncTearDown()

    async def test_postgres_branch_executes(self) -> None:
        """Runs the exact block that carried an unbound name for three commits."""
        from yuyutsava.daemon.bootstrap import build_retrieval
        from yuyutsava.storage.factory import StoreFactory

        stores = StoreFactory(
            self.settings, pg_pool=self.pool, embedder=self.embedder,
        )
        rtv = await build_retrieval(
            home=self.home, stores=stores, mem_settings=self.mem,
            pg_pool=self.pool, embedder=self.embedder,
        )
        self.assertIsNotNone(
            rtv.note_index,
            "the pgvector note index was not built even though Postgres and an "
            "embedder are both live — the todo_recall path is dead",
        )
        self.assertTrue(
            rtv.skill_store.supports_semantic_search,
            "Postgres + embedder produced a keyword-only skill store; skill "
            "recall would silently degrade to word overlap",
        )

    async def test_note_index_is_installed_as_the_default(self) -> None:
        """The exchange's embed-on-write hooks resolve this singleton."""
        from yuyutsava.daemon.bootstrap import build_retrieval
        from yuyutsava.storage.factory import StoreFactory
        from yuyutsava.todoboard.recall import get_default_note_index

        await build_retrieval(
            home=self.home,
            stores=StoreFactory(self.settings, pg_pool=self.pool, embedder=self.embedder),
            mem_settings=self.mem, pg_pool=self.pool, embedder=self.embedder,
        )
        self.assertIsNotNone(
            get_default_note_index(),
            "set_default_note_index was never called, so every embed-on-write "
            "hook silently no-ops and todo_recall returns nothing",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
