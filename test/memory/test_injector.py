"""Unit tests for MemoryInjector budget fitting (whole-memory truncation).

Run:  uv run python -m unittest test.memory.test_injector -v
"""

from __future__ import annotations

import unittest

from yuyutsava.context.injector import MemoryInjector
from yuyutsava.core.config import LIMITS
from yuyutsava.memory.store import MemoryHit, MemoryStore

_BUDGET = LIMITS.max_memory_chars


class _FakeStore(MemoryStore):
    def __init__(self, hits: list[MemoryHit]) -> None:
        self._hits = hits

    async def add(self, **_kw) -> str:  # pragma: no cover - unused
        return "mem_x"

    async def search(self, query, k=5, kinds=None):
        return self._hits[:k]


class MemoryInjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_when_no_hits(self) -> None:
        inj = MemoryInjector(_FakeStore([]))
        self.assertEqual(await inj.build_block("task"), "")

    async def test_empty_when_blank_task(self) -> None:
        inj = MemoryInjector(_FakeStore([MemoryHit("m1", "fact", "alpha", 0.9)]))
        self.assertEqual(await inj.build_block("   "), "")

    async def test_renders_hits_within_budget(self) -> None:
        hits = [
            MemoryHit("m1", "fact", "alpha", 0.9),
            MemoryHit("m2", "summary", "beta", 0.8),
        ]
        block = await MemoryInjector(_FakeStore(hits)).build_block("task")
        self.assertIn("alpha", block)
        self.assertIn("beta", block)
        self.assertLessEqual(len(block), _BUDGET)

    async def test_drops_whole_memory_not_fragment(self) -> None:
        # First memory nearly fills the budget; the second cannot fit and must
        # be dropped entirely rather than appended as a fragment.
        hits = [
            MemoryHit("m1", "fact", "a" * (_BUDGET - 300), 0.9),
            MemoryHit("m2", "fact", "DROPME" + "b" * 400, 0.8),
        ]
        block = await MemoryInjector(_FakeStore(hits)).build_block("task")
        self.assertIn("a" * 100, block)       # first memory present, whole
        self.assertNotIn("DROPME", block)     # second dropped, not fragmented
        self.assertLessEqual(len(block), _BUDGET)

    async def test_oversized_first_memory_truncated(self) -> None:
        block = await MemoryInjector(
            _FakeStore([MemoryHit("m1", "fact", "z" * (_BUDGET * 2), 0.9)])
        ).build_block("task")
        self.assertLessEqual(len(block), _BUDGET)
        self.assertIn("z", block)


if __name__ == "__main__":
    unittest.main()
