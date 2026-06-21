"""Unit tests for the generic RetrievalInjector + SkillInjector.

Run:  uv run python -m unittest test.retrieval.test_injector -v
"""

from __future__ import annotations

import unittest

from yuyutsava.core.config import LIMITS
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.injector import RetrievalInjector
from yuyutsava.skills.injector import SkillInjector


class _RecordingStore:
    """Captures the kwargs the injector forwards to search."""

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.last_kwargs: dict | None = None

    async def search(self, query, k=5, **kwargs):
        self.last_kwargs = {"k": k, **kwargs}
        return self._hits[:k]


class RetrievalInjectorTests(unittest.IsolatedAsyncioTestCase):
    def _inj(self, store, **kw):
        return RetrievalInjector(
            store, top_k=5, prefix="PREFIX:", budget_chars=200,
            render=lambda h: f"  - {h.text}", **kw,
        )

    async def test_empty_when_no_hits(self) -> None:
        self.assertEqual(await self._inj(_RecordingStore([])).build_block("task"), "")

    async def test_empty_when_blank_task(self) -> None:
        store = _RecordingStore([Hit("1", "alpha", 0.9)])
        self.assertEqual(await self._inj(store).build_block("   "), "")

    async def test_renders_and_caps_to_budget(self) -> None:
        hits = [Hit("1", "alpha", 0.9), Hit("2", "beta", 0.8)]
        block = await self._inj(_RecordingStore(hits)).build_block("task")
        self.assertIn("PREFIX:", block)
        self.assertIn("alpha", block)
        self.assertIn("beta", block)

    async def test_drops_whole_entry_over_budget(self) -> None:
        hits = [Hit("1", "a" * 150, 0.9), Hit("2", "DROPME" + "b" * 150, 0.8)]
        block = await self._inj(_RecordingStore(hits)).build_block("task")
        self.assertIn("a" * 50, block)
        self.assertNotIn("DROPME", block)
        self.assertLessEqual(len(block), 200)

    async def test_forwards_search_kwargs(self) -> None:
        store = _RecordingStore([Hit("1", "alpha", 0.9)])
        inj = self._inj(store, search_kwargs={"agent": "orchestrator"})
        await inj.build_block("task")
        self.assertEqual(store.last_kwargs.get("agent"), "orchestrator")


class SkillInjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_skill_name_and_passes_agent(self) -> None:
        store = _RecordingStore([
            Hit("pdf-x", "edit pdfs", 0.9, payload={"name": "pdf-x"}),
        ])
        block = await SkillInjector(store, agent="orchestrator").build_block("edit a pdf")
        self.assertIn("RELEVANT SKILLS", block)
        self.assertIn("pdf-x: edit pdfs", block)
        self.assertEqual(store.last_kwargs.get("agent"), "orchestrator")
        self.assertLessEqual(len(block), LIMITS.max_skill_index_chars)


if __name__ == "__main__":
    unittest.main()
