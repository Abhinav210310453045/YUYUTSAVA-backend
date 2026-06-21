"""Unit tests for the SQLite keyword-fallback skill store + indexer.

Run:  uv run python -m unittest test.skills.test_store -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# Import the store first: it pulls in the memory.embedder→core chain, so core
# is fully initialized before skills.registry loads (avoids an import-order cycle
# when this module is imported standalone, before core).
from yuyutsava.skills.store import SkillIndexer, SqliteSkillStore
from yuyutsava.skills.registry import SkillMeta


def _meta(name: str, desc: str, *, scope: str = "personal", agent: str | None = None) -> SkillMeta:
    return SkillMeta(
        name=name, description=desc, path=Path(f"/tmp/{name}/SKILL.md"),
        scope=scope, agent=agent,
    )


class SqliteSkillStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteSkillStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_upsert_and_keyword_search(self) -> None:
        await self.store.upsert(
            _meta("pdf-manipulation-overview",
                  "Read, modify, and save PDF files using Python libraries"),
            body="full body",
        )
        await self.store.upsert(
            _meta("downloads-organizer", "Sort the downloads folder by file type"),
            body="full body",
        )
        hits = await self.store.search("how do I edit a pdf")
        self.assertTrue(hits)
        self.assertEqual(hits[0].payload["name"], "pdf-manipulation-overview")
        # Hit shape: payload carries name/scope/agent for the injector to render.
        self.assertEqual(hits[0].id, "pdf-manipulation-overview")
        self.assertIn("scope", hits[0].payload)

    async def test_upsert_is_idempotent(self) -> None:
        await self.store.upsert(_meta("s", "first description about apples"), body="b1")
        await self.store.upsert(_meta("s", "second description about apples"), body="b2")
        self.assertEqual(await self.store.all_names(), {"s"})
        hits = await self.store.search("apples")
        self.assertEqual(hits[0].text, "second description about apples")

    async def test_agent_filter(self) -> None:
        await self.store.upsert(_meta("universal", "shared apricot skill", agent=None), body="b")
        await self.store.upsert(
            _meta("triage-only", "apricot triage skill", agent="triage"), body="b")
        # agent=orchestrator sees universal (agent NULL) but not triage's skill.
        hits = await self.store.search("apricot", agent="orchestrator")
        names = {h.payload["name"] for h in hits}
        self.assertIn("universal", names)
        self.assertNotIn("triage-only", names)

    async def test_no_match_returns_empty(self) -> None:
        await self.store.upsert(_meta("s", "something"), body="b")
        self.assertEqual(await self.store.search("zzz unfindable"), [])

    async def test_indexer_syncs_only_missing(self) -> None:
        class _FakeRegistry:
            def scan(self, agent=None):
                return [_meta("a", "alpha skill"), _meta("b", "beta skill")]

            def get_body(self, name):
                return f"body of {name}"

        await self.store.upsert(_meta("a", "alpha skill"), body="body of a")
        added = await SkillIndexer.sync(_FakeRegistry(), self.store)
        self.assertEqual(added, 1)  # only 'b' was missing
        self.assertEqual(await self.store.all_names(), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
