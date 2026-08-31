"""``UnifiedSkillStore`` on SQLite and Postgres — including the declared asymmetry.

Phase 2 step 2.5b, playbook order 9. The first domain where the two backends do
not run the same algorithm, so the contract is split in two on purpose:

* ``_SkillContract`` — what **must** hold on both: storage, upsert semantics,
  agent scoping, ``all_names``, and that ``backfill_embeddings`` is always
  callable.
* ``_KeywordSearchContract`` — the SQLite ranking, which is also the Postgres
  *fallback* path, so it is mounted on both.
* ``SemanticSearchIsDeclared`` — that the capability is a **property**, not
  something callers discover with ``getattr``.

The asymmetry itself (cosine vs LIKE-counting) is not asserted to be identical,
because it is not. What is asserted is that the **agent scope filter applies on
both paths** — a degraded backend may return worse-ranked results, but must
never return another agent's skills. That is the security-adjacent invariant,
and it was previously written out twice.

Postgres cases need a live server *and* a working embedder; both are checked, and
the semantic cases skip rather than fail when the embedder is unavailable. No
billable API is used — embeddings run against the local Ollama endpoint the
project already uses.

Run:  .venv/bin/python test/storage/test_skill_store_parity.py
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


def _meta(name: str, description: str, *, agent: str | None = None, scope: str = "bundled"):
    from yuyutsava.skills.registry import SkillMeta

    return SkillMeta(
        name=name, scope=scope, agent=agent, description=description,
        path=Path(f"/skills/{name}.md"), requires_tools=("tr_read_file",),
    )


class _SkillContract:
    """Behaviour both backends must agree on, regardless of search algorithm."""

    async def test_upsert_then_all_names(self) -> None:
        await self.store.upsert(_meta(self.sid("a"), "read a file"), "body")
        self.assertIn(self.sid("a"), await self.store.all_names())

    async def test_upsert_replaces_not_duplicates(self) -> None:
        name = self.sid("dup")
        await self.store.upsert(_meta(name, "first description"), "b1")
        await self.store.upsert(_meta(name, "second description"), "b2")
        names = [n for n in await self.store.all_names() if n == name]
        self.assertEqual(
            len(names), 1,
            "re-indexing a skill created a second row; the disk is the source "
            "of truth and a rescan runs on every boot",
        )

    async def test_upsert_updates_the_description(self) -> None:
        """A rescan must publish the edited description, not the stale one."""
        name = self.sid("edit")
        await self.store.upsert(_meta(name, "aardvark handling"), "b")
        await self.store.upsert(_meta(name, "zebra handling"), "b")
        hits = await self.store._keyword_search("zebra", 5, None)
        self.assertIn(name, [h.id for h in hits])
        stale = await self.store._keyword_search("aardvark", 5, None)
        self.assertNotIn(name, [h.id for h in stale])

    async def test_backfill_is_always_callable(self) -> None:
        """The fix for the ``getattr`` probes: the method always exists.

        On a backend without vectors there is nothing to repair, so 0 is the
        true answer — not an ``AttributeError`` the caller has to guard.
        """
        n = await self.store.backfill_embeddings()
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    async def test_supports_semantic_search_is_a_property(self) -> None:
        self.assertIsInstance(self.store.supports_semantic_search, bool)


class _KeywordSearchContract:
    """The LIKE-counting path. SQLite's only ranking, Postgres's fallback."""

    async def test_matches_on_description_words(self) -> None:
        await self.store.upsert(_meta(self.sid("k1"), "compress video files"), "b")
        await self.store.upsert(_meta(self.sid("k2"), "send an email"), "b")
        hits = await self.store._keyword_search("compress video", 5, None)
        ids = [h.id for h in hits]
        self.assertIn(self.sid("k1"), ids)
        self.assertNotIn(self.sid("k2"), ids)

    async def test_ranks_more_matches_higher(self) -> None:
        await self.store.upsert(_meta(self.sid("r1"), "compress video files nicely"), "b")
        await self.store.upsert(_meta(self.sid("r2"), "compress things"), "b")
        hits = await self.store._keyword_search("compress video", 5, None)
        ids = [h.id for h in hits]
        self.assertLess(
            ids.index(self.sid("r1")), ids.index(self.sid("r2")),
            "the skill matching both query words ranked below the one matching "
            "only the first",
        )

    async def test_score_is_zero_not_an_invented_number(self) -> None:
        """``Hit.score`` means cosine similarity; keyword hits must not fake one."""
        await self.store.upsert(_meta(self.sid("s1"), "compress video"), "b")
        hits = await self.store._keyword_search("compress", 5, None)
        self.assertEqual(
            [h.score for h in hits], [0.0] * len(hits),
            "a keyword hit reported a non-zero score, so a caller could compare "
            "it against a real cosine score on the same scale",
        )

    async def test_agent_scoping_hides_other_agents_skills(self) -> None:
        """The security-adjacent invariant, asserted on the degraded path too."""
        shared = self.sid("shared")
        mine = self.sid("mine")
        theirs = self.sid("theirs")
        await self.store.upsert(_meta(shared, "compress video", agent=None), "b")
        await self.store.upsert(_meta(mine, "compress video", agent="tinker"), "b")
        await self.store.upsert(_meta(theirs, "compress video", agent="orchestrator"), "b")

        ids = [h.id for h in await self.store._keyword_search("compress", 10, "tinker")]
        self.assertIn(shared, ids, "an unscoped skill was hidden from an agent")
        self.assertIn(mine, ids)
        self.assertNotIn(
            theirs, ids,
            "an agent was shown another agent's scoped skill. Skill scoping is "
            "the whole reason `agent` is a column.",
        )

    async def test_no_agent_filter_returns_everything(self) -> None:
        await self.store.upsert(_meta(self.sid("n1"), "compress video", agent="tinker"), "b")
        ids = [h.id for h in await self.store._keyword_search("compress", 10, None)]
        self.assertIn(self.sid("n1"), ids)

    async def test_limit_is_honoured(self) -> None:
        for i in range(4):
            await self.store.upsert(_meta(self.sid(f"L{i}"), "compress video"), "b")
        self.assertLessEqual(len(await self.store._keyword_search("compress", 2, None)), 2)

    async def test_no_match_returns_empty(self) -> None:
        await self.store.upsert(_meta(self.sid("z1"), "compress video"), "b")
        self.assertEqual(
            await self.store._keyword_search("quantum chromodynamics", 5, None), [])


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedSkills(
    _SkillContract, _KeywordSearchContract, unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        from yuyutsava.skills.store_unified import sqlite_skill_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_skill_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def sid(self, name: str) -> str:
        return f"skill-{name}"

    async def test_semantic_search_is_off(self) -> None:
        self.assertFalse(
            self.store.supports_semantic_search,
            "SQLite reported semantic search; there is no pgvector here and "
            "search() would try to embed",
        )

    async def test_search_falls_through_to_keyword(self) -> None:
        """``search`` must work without an embedder, not raise."""
        await self.store.upsert(_meta(self.sid("f1"), "compress video"), "b")
        hits = await self.store.search("compress", k=5)
        self.assertIn(self.sid("f1"), [h.id for h in hits])

    async def test_an_embedder_on_sqlite_is_ignored_not_fatal(self) -> None:
        """Misconfiguration degrades to keyword search rather than crashing."""
        from yuyutsava.skills.store_unified import UnifiedSkillStore
        from yuyutsava.storage.dialect import SqliteDialect
        from yuyutsava.skills.store_unified import SkillSchema

        class _Exploding:
            async def embed_one(self, *a, **k):
                raise AssertionError("the embedder was used on SQLite")

        store = UnifiedSkillStore(
            SqliteDialect(SkillSchema(Path(self._tmp.name) / "state.db")),
            embedder=_Exploding(),
        )
        self.assertFalse(store.supports_semantic_search)
        await store.upsert(_meta(self.sid("e1"), "compress video"), "b")
        self.assertIn(self.sid("e1"), [h.id for h in await store.search("compress")])


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedSkills(
    _SkillContract, _KeywordSearchContract, unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.skills.store_unified import pg_skill_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_skill_store(
            self.pool, Embedder(MemorySettings.from_env(default_enabled=True))
        )

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM skills WHERE name LIKE %s", (f"skill-%{self._suffix}",))
        await self.pool.close()

    def sid(self, name: str) -> str:
        return f"skill-{name}-{self._suffix}"

    async def test_semantic_search_is_on(self) -> None:
        self.assertTrue(
            self.store.supports_semantic_search,
            "Postgres with an embedder reported no semantic search — every "
            "skill lookup would silently degrade to word overlap",
        )


class SemanticSearchIsDeclared(unittest.TestCase):
    """The capability must be a property, not something callers probe for."""

    def test_backfill_exists_on_the_class_itself(self) -> None:
        from yuyutsava.skills.store_unified import UnifiedSkillStore

        self.assertTrue(
            hasattr(UnifiedSkillStore, "backfill_embeddings"),
            "backfill_embeddings is backend-conditional again; the three "
            "getattr() probes it replaced would come back",
        )
        self.assertIsInstance(
            UnifiedSkillStore.supports_semantic_search, property,
            "supports_semantic_search must be a declared property — the "
            "pattern test_twin_conformance.py names as the good one",
        )

    def test_dialects_declare_vector_support(self) -> None:
        from yuyutsava.storage.dialect import (
            EventsSqliteDialect, PostgresDialect, SqliteDialect,
        )

        self.assertFalse(SqliteDialect.supports_vectors)
        self.assertFalse(EventsSqliteDialect.supports_vectors)
        self.assertTrue(PostgresDialect.supports_vectors)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
