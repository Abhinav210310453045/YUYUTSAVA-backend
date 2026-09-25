"""Stores for tests that build agents, apps or subagents.

The builders take their board and visual store explicitly (`R-GLB-01`); a test
that only needs the agent to assemble hands it these. Neither touches the real
board or the state database: the board is in memory, and the visual store is a
SQLite file in a scratch directory that is created lazily, if ever.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from test.todoboard.fake_store import FakeTodoStore
from yuyutsava.todoboard.exchange import TodoExchange
from yuyutsava.visuals.store_unified import sqlite_visual_store


def fake_board() -> TodoExchange:
    """A TODO board over an in-memory store."""
    return TodoExchange(FakeTodoStore())


def scratch_visual_store():
    """A visual store in a fresh scratch directory."""
    return sqlite_visual_store(Path(tempfile.mkdtemp(prefix="yy-test-visuals-")) / "visuals.db")
