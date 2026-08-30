"""``UnifiedMemoryStore`` on SQLite and Postgres, asymmetry declared.

Phase 2 step 2.5b, playbook order 12. Written **before** the unified store.

Same shape as skills (finding AF): the *storage* is shared, the *retrieval* is
not — Postgres ranks by pgvector cosine, SQLite counts ``LIKE`` matches. What is
unified is everything else, and what is declared is the difference.

This is the migration that finally lets the three
``getattr(store, "backfill_embeddings", None)`` probes go, because
``SqliteMemoryStore`` was the last store lacking the method.

Two divergences beyond the search algorithm:

**``created_ts`` came from two different clocks** — SQLite passed
``time.time()``, Postgres omitted the column and let ``DEFAULT now()`` fire.
Third domain in a row (transcripts, feedback, now memory). It matters here
because ``created_ts DESC`` is the *tiebreaker* in keyword ranking, so two
memories written a moment apart could order differently per backend.

**Near-duplicate suppression is Postgres-only** — it needs cosine similarity.
SQLite stores every repeat. That is inherent, so it is asserted as a declared
difference rather than papered over.

Postgres cases need a live server and a working embedder; no billable API is
touched (embeddings run against the local Ollama endpoint).

Run:  .venv/bin/python test/storage/test_memory_store_parity.py
"""

from __future__ import annotations

# NOTE: helpers below use the raw pool connection, which yields TUPLES.
# They read positionally on purpose — reading by name only worked while the
# dialect was leaking `dict_row` onto pooled connections (finding AT).

import os
import socket
import tempfile
import time
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


class _MemoryContract:
    """Behaviour both backends must agree on, regardless of ranking."""

    async def test_add_returns_a_prefixed_id(self) -> None:
        mid = await self.store.add(kind="fact", text=self.txt("the sky is blue"))
        self.assertTrue(mid.startswith("mem_"))

    async def test_added_memory_is_findable(self) -> None:
        text = self.txt("aardvarks enjoy termites")
        await self.store.add(kind="fact", text=text)
        hits = await self.store._keyword_search("aardvarks termites", 5, None)
        self.assertIn(text, [h.text for h in hits])

    async def test_kind_is_preserved(self) -> None:
        await self.store.add(kind="preference", text=self.txt("prefers dark mode"))
        hits = await self.store._keyword_search("dark mode", 5, None)
        self.assertEqual(
            [h.kind for h in hits if h.text == self.txt("prefers dark mode")],
            ["preference"],
        )

    async def test_unknown_kind_falls_back_to_fact(self) -> None:
        """Invalid kinds are coerced, not rejected — recall must never lose data."""
        await self.store.add(kind="not_a_kind", text=self.txt("coerced kind"))
        hits = await self.store._keyword_search("coerced kind", 5, None)
        self.assertEqual([h.kind for h in hits], ["fact"])

    async def test_kind_filter_excludes_other_kinds(self) -> None:
        """``kinds`` is how purge keeps facts and drops ephemera — it must bind."""
        await self.store.add(kind="fact", text=self.txt("durable zebra knowledge"))
        await self.store.add(kind="summary", text=self.txt("ephemeral zebra summary"))
        hits = await self.store._keyword_search("zebra", 10, ("fact",))
        kinds = {h.kind for h in hits}
        self.assertIn("fact", kinds)
        self.assertNotIn(
            "summary", kinds,
            "the kind filter did not bind. Postgres used `kind = ANY(%s)` and "
            "SQLite `kind IN (?,...)`; only one portable form can be right.",
        )

    async def test_multiple_kinds_filter(self) -> None:
        for kind in ("fact", "summary", "task_outcome"):
            await self.store.add(kind=kind, text=self.txt(f"quokka {kind}"))
        hits = await self.store._keyword_search("quokka", 10, ("fact", "summary"))
        self.assertEqual({h.kind for h in hits}, {"fact", "summary"})

    async def test_no_kind_filter_returns_all(self) -> None:
        for kind in ("fact", "summary"):
            await self.store.add(kind=kind, text=self.txt(f"lemur {kind}"))
        hits = await self.store._keyword_search("lemur", 10, None)
        self.assertEqual({h.kind for h in hits}, {"fact", "summary"})

    async def test_metadata_is_optional(self) -> None:
        self.assertTrue(
            (await self.store.add(kind="fact", text=self.txt("no metadata"))).startswith("mem_")
        )
        self.assertTrue(
            (await self.store.add(
                kind="fact", text=self.txt("with metadata"),
                metadata={"src": "cli", "n": 2})).startswith("mem_")
        )

    async def test_keyword_ranks_more_matches_higher(self) -> None:
        await self.store.add(kind="fact", text=self.txt("compress video files nicely"))
        await self.store.add(kind="fact", text=self.txt("compress things"))
        hits = await self.store._keyword_search("compress video", 5, None)
        texts = [h.text for h in hits]
        self.assertLess(
            texts.index(self.txt("compress video files nicely")),
            texts.index(self.txt("compress things")),
        )

    async def test_keyword_score_is_zero(self) -> None:
        """``score`` means cosine similarity; a keyword hit must not invent one."""
        await self.store.add(kind="fact", text=self.txt("compress video"))
        hits = await self.store._keyword_search("compress", 5, None)
        self.assertEqual([h.score for h in hits], [0.0] * len(hits))

    async def test_keyword_limit_is_honoured(self) -> None:
        for i in range(4):
            await self.store.add(kind="fact", text=self.txt(f"wombat {i}"))
        self.assertLessEqual(len(await self.store._keyword_search("wombat", 2, None)), 2)

    async def test_no_match_returns_empty(self) -> None:
        await self.store.add(kind="fact", text=self.txt("compress video"))
        self.assertEqual(
            await self.store._keyword_search("quantum chromodynamics zzz", 5, None), [])

    async def test_backfill_is_always_callable(self) -> None:
        """The whole reason this domain unblocks the ``getattr`` probe removal."""
        n = await self.store.backfill_embeddings()
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    async def test_created_ts_comes_from_the_application(self) -> None:
        """One clock. It is the tiebreaker in keyword ranking on both backends.

        Postgres previously omitted the column and let ``DEFAULT now()`` fire —
        the database server's clock — so two memories written moments apart
        could tie-break differently per backend.
        """
        before = time.time()
        await self.store.add(kind="fact", text=self.txt("clock check"))
        after = time.time()
        ts = await self.created_ts_of(self.txt("clock check"))
        self.assertGreaterEqual(ts, before - 5)
        self.assertLessEqual(ts, after + 5)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedMemory(_MemoryContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.store_unified import sqlite_memory_store

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.db"
        self.store = sqlite_memory_store(self.db)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def txt(self, s: str) -> str:
        return s

    async def created_ts_of(self, text: str) -> float:
        async with self.store._d.reading() as conn:
            cur = await conn.execute(
                "SELECT created_ts FROM memories WHERE text = ?", (text,))
            return float((await cur.fetchone())[0])

    async def test_semantic_search_is_off(self) -> None:
        self.assertFalse(self.store.supports_semantic_search)

    async def test_search_falls_through_to_keyword(self) -> None:
        await self.store.add(kind="fact", text="compress video")
        self.assertIn("compress video",
                      [h.text for h in await self.store.search("compress", k=5)])

    async def test_duplicates_are_kept_without_vectors(self) -> None:
        """Declared difference: dedup needs cosine similarity, so SQLite has none."""
        for _ in range(3):
            await self.store.add(kind="summary", text="the same summary twice over")
        hits = await self.store._keyword_search("same summary", 10, None)
        self.assertEqual(
            len(hits), 3,
            "SQLite silently deduplicated; it has no way to measure similarity, "
            "so this could only be a text-equality shortcut that Postgres does "
            "not share",
        )


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedMemory(_MemoryContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.memory.store_unified import pg_memory_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_memory_store(
            self.pool, Embedder(MemorySettings.from_env(default_enabled=True))
        )

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE text LIKE %s", (f"%[{self._suffix}]",))
        await self.pool.close()

    def txt(self, s: str) -> str:
        return f"{s} [{self._suffix}]"

    async def created_ts_of(self, text: str) -> float:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT extract(epoch FROM created_ts) AS ts FROM memories "
                "WHERE text = %s", (text,))
            return float((await cur.fetchone())[0])

    async def test_semantic_search_is_on(self) -> None:
        self.assertTrue(
            self.store.supports_semantic_search,
            "Postgres with an embedder reported no semantic search — memory "
            "recall would silently degrade to word overlap",
        )

    async def test_source_thread_parent_row_is_created(self) -> None:
        """``memories.source_thread_id`` FKs to threads (ON DELETE SET NULL)."""
        thread = f"thr-mem-{self._suffix}"
        await self.store.add(
            kind="fact", text=self.txt("has a thread"), source_thread_id=thread)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM threads WHERE thread_id = %s", (thread,))
            self.assertEqual((await cur.fetchone())[0], 1)
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM threads WHERE thread_id = %s", (thread,))


class SemanticCapabilityIsDeclared(unittest.TestCase):
    """No caller should have to probe for ``backfill_embeddings`` any more."""

    def test_backfill_exists_on_the_class(self) -> None:
        from yuyutsava.memory.store_unified import UnifiedMemoryStore

        self.assertTrue(hasattr(UnifiedMemoryStore, "backfill_embeddings"))
        self.assertIsInstance(
            UnifiedMemoryStore.supports_semantic_search, property)

    def test_no_getattr_probes_remain_in_production(self) -> None:
        """The payoff: memory was the last store missing the method.

        With both vector stores declaring it unconditionally, the three
        ``getattr(store, "backfill_embeddings", None)`` sites can call directly.
        """
        import ast
        import pathlib

        # AST, not a line regex: several modules *describe* the retired pattern
        # in their docstrings, and a text match would flag the documentation of
        # the fix as the defect. Only real calls count.
        root = pathlib.Path(__file__).resolve().parents[2] / "yuyutsava"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "getattr"):
                    continue
                if any(isinstance(a, ast.Constant) and a.value == "backfill_embeddings"
                       for a in node.args):
                    offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "backfill_embeddings is still discovered by getattr:\n  "
            + "\n  ".join(offenders)
            + "\nEvery store that can have it now declares it, so a probe here "
              "means a call site was missed — and a future one that forgets the "
              "guard will AttributeError on SQLite only, in production only.",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
