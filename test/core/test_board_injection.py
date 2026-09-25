"""The TODO board is handed over, never looked up.

R-BUG-18 / `R-GLB-01`. The board used to be a process default that every chat
stack the daemon built re-pointed at a plain Postgres store (and a fresh note
index), silently dropping the daemon's SQLite spillover. Now a master gets the
board it is given, a builder refuses to wire the todo_* family without one, and
a stack without a daemon builds its own.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from test.core.agent_fingerprint import capture_cli
from test.todoboard.fake_store import FakeTodoStore
from yuyutsava.core import engine
from yuyutsava.core.stores import StoreFactory, standalone_board
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.todoboard.exchange import TodoExchange


class AMasterNeedsItsBoard(unittest.TestCase):
    def test_no_board_is_an_error_not_a_silent_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "todo_\\* family but no board"):
            capture_cli(board=None)

    def test_the_board_given_is_the_one_its_tools_use(self) -> None:
        board, seen = TodoExchange(FakeTodoStore()), []
        real = engine.make_todo_tools

        def spy(exchange, **kwargs):
            seen.append(exchange)
            return real(exchange, **kwargs)

        with mock.patch.object(engine, "make_todo_tools", spy):
            capture_cli(board=board)
        self.assertEqual(seen, [board])


class AStandaloneStackBuildsItsOwn(unittest.TestCase):
    def test_on_sqlite_it_is_the_factorys_store_and_no_index(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": tmp}),
        ):
            factory = StoreFactory(StorageSettings(backend="sqlite"))
            stores = SimpleNamespace(pg_pool=None, embedder=None, factory=factory)
            board = standalone_board(stores)  # type: ignore[arg-type]
        self.assertIsInstance(board, TodoExchange)
        self.assertIsNone(board.note_index)


if __name__ == "__main__":
    unittest.main()
