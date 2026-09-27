"""``build_retrieval`` — the skills index and the board's note recall, at boot.

The TODO-note recall index only exists on Postgres with an embedder, and while
it was built inside ``build_daemon`` nothing could reach it: ``build_daemon``
starts uvicorn, the LangGraph host and MCP servers, so it never returns in a
test. A ``NameError`` sat in that branch undetected while every SQLite suite
stayed green. Extracting ``build_retrieval`` is what made it testable; these
tests execute it rather than analyse it.

The note index is built with the board (``build_storage``) and travels on it;
``build_retrieval`` returns that index and boot-syncs it against the board.
Both backends are covered on purpose: on SQLite the index is correctly
**absent**, which is as much a behaviour as building it.

Everything writes under a scratch ``YUYUTSAVA_HOME``, and the board is an
in-memory one — never the real board.

Run:  .venv/bin/python test/daemon/test_build_retrieval.py
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from test.todoboard.fake_store import FakeTodoStore
from yuyutsava.core.stores import StoreFactory
from yuyutsava.daemon.bootstrap import build_retrieval
from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.embedder import Embedder
from yuyutsava.storage.backend import DEFAULT_PG_DSN, StorageSettings
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.todoboard.exchange import TodoExchange
from yuyutsava.todoboard.recall import TodoNoteIndex


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


class _RecordingIndex:
    """A note index that records the boards it was asked to sync."""

    def __init__(self, *, fail: bool = False) -> None:
        self.synced: list[TodoExchange] = []
        self._fail = fail

    async def sync(self, exchange: TodoExchange) -> int:
        self.synced.append(exchange)
        if self._fail:
            raise RuntimeError("pgvector is gone")
        return 0


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.workspace = self.home / "ws"
        self.workspace.mkdir()
        self._env = mock.patch.dict(
            os.environ, {"YUYUTSAVA_HOME": self._tmp.name, "YUYUTSAVA_STORAGE_BACKEND": "sqlite"}
        )
        self._env.start()

    async def asyncTearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    async def _build(self, board: TodoExchange, **kw):
        kw.setdefault("stores", StoreFactory(StorageSettings(backend="sqlite")))
        kw.setdefault("mem_settings", MemorySettings.from_env(default_enabled=False))
        kw.setdefault("pg_pool", None)
        kw.setdefault("embedder", None)
        return await build_retrieval(home=self.home, workspace=self.workspace, board=board, **kw)


class RetrievalOnSqlite(_Base):
    """No pgvector: skills fall back to the keyword twin, no note index."""

    async def test_builds_without_postgres(self) -> None:
        rtv = await self._build(TodoExchange(FakeTodoStore()))
        self.assertIsNotNone(rtv.skill_registry)
        self.assertFalse(
            rtv.skill_store.supports_semantic_search,
            "the skill store claims semantic search without pgvector — every "
            "lookup would try to embed and fall back on each call",
        )
        self.assertIsNone(
            rtv.note_index,
            "a note index appeared without pgvector; the exchange's "
            "embed-on-write hooks expect None here and would try to embed",
        )

    async def test_skill_registry_sees_bundled_skills(self) -> None:
        rtv = await self._build(TodoExchange(FakeTodoStore()))
        bundled = [s for s in rtv.skill_registry.scan() if s.scope == "bundled"]
        self.assertGreater(len(bundled), 0, "no bundled skills discovered")

    async def test_workspace_skills_are_scanned(self) -> None:
        """Without the workspace the registry fell back to the process cwd."""
        skill = WorkspaceLayout.for_workspace(self.workspace).skills / "ws-only-probe"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: ws-only-probe\ndescription: a workspace skill\n---\nbody\n",
            encoding="utf-8",
        )
        rtv = await self._build(TodoExchange(FakeTodoStore()))
        found = [s.name for s in rtv.skill_registry.scan() if s.scope == "workspace"]
        self.assertIn("ws-only-probe", found)


class TheBoardsIndexIsSyncedAtBoot(_Base):
    async def test_the_boards_own_index_is_returned_and_synced(self) -> None:
        index = _RecordingIndex()
        board = TodoExchange(FakeTodoStore(), note_index=index)
        rtv = await self._build(board)
        self.assertIs(rtv.note_index, index)
        self.assertEqual(index.synced, [board])

    async def test_a_failing_sync_is_logged_not_fatal(self) -> None:
        index = _RecordingIndex(fail=True)
        board = TodoExchange(FakeTodoStore(), note_index=index)
        with self.assertLogs("yuyutsava.daemon.bootstrap", "WARNING") as logs:
            rtv = await self._build(board)
        self.assertIs(rtv.note_index, index)
        self.assertIn("boot index sync failed", logs.output[0])


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class RetrievalOnPostgres(_Base):
    """**The branch the NameError hid in**, executed against a real pool."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings = StorageSettings(backend="postgres", pg_dsn=_pg_dsn())
        self.pool = PgPool(self.settings)
        await self.pool.open()
        self.mem = MemorySettings.from_env(default_enabled=True)
        self.embedder = Embedder(self.mem)

    async def asyncTearDown(self) -> None:
        await self.embedder.aclose()
        await self.pool.close()
        await super().asyncTearDown()

    async def test_postgres_branch_executes(self) -> None:
        index = TodoNoteIndex(self.pool, embedder=self.embedder)
        rtv = await self._build(
            TodoExchange(FakeTodoStore(), note_index=index),
            stores=StoreFactory(self.settings, pg_pool=self.pool, embedder=self.embedder),
            mem_settings=self.mem,
            pg_pool=self.pool,
            embedder=self.embedder,
        )
        self.assertIs(
            rtv.note_index,
            index,
            "the board's pgvector note index was not the one returned — the "
            "todo_recall path would search an index nothing writes to",
        )
        self.assertTrue(
            rtv.skill_store.supports_semantic_search,
            "Postgres + embedder produced a keyword-only skill store; skill "
            "recall would silently degrade to word overlap",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
