"""``UnifiedArtifactStore`` on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 13. Written **before** the unified store.

Artifacts hold offloaded tool results, so ``put`` sits on the hot path of every
oversized tool call and ``get`` is always a windowed read. The slicing maths is
where a quiet bug would live — an off-by-one in ``offset``/``length`` returns
the wrong region of a file to the agent without anything failing — so it gets
most of the coverage here.

``created_ts`` gets its own test for the fourth time (transcripts AE, feedback
AH, memory AI, now artifacts): SQLite passed ``time.time()``, Postgres omitted
the column and let ``DEFAULT now()`` fire. It decides the TTL sweep boundary via
``delete_older_than``.

``supports_recall`` was already the *declared capability* pattern the review
holds up as correct, so the tests here pin that it stayed declared — and that
``recall`` degrades to ``[]`` rather than raising when a caller skips the check.

Run:  .venv/bin/python test/storage/test_artifact_store_parity.py
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


class _ArtifactContract:
    """Behaviour both backends must agree on."""

    async def test_put_returns_a_prefixed_id(self) -> None:
        aid = await self.store.put(self.tid("a"), "tr_read_file", "hello")
        self.assertTrue(aid.startswith("art_"))

    async def test_roundtrip(self) -> None:
        aid = await self.store.put(self.tid("round"), "tr_read_file", "hello world")
        got = await self.store.get(aid)
        self.assertEqual(got.content, "hello world")
        self.assertEqual(got.artifact_id, aid)
        self.assertEqual(got.total_chars, 11)
        self.assertEqual(got.offset, 0)

    async def test_missing_is_none(self) -> None:
        self.assertIsNone(await self.store.get("art_does_not_exist"))

    async def test_get_is_windowed(self) -> None:
        body = "".join(str(i % 10) for i in range(100))
        aid = await self.store.put(self.tid("win"), "t", body)
        got = await self.store.get(aid, offset=10, length=5)
        self.assertEqual(got.content, body[10:15])
        self.assertEqual(got.offset, 10)
        self.assertEqual(
            got.total_chars, 100,
            "total_chars must be the FULL size, not the slice — the agent uses "
            "it to decide whether to page further",
        )

    async def test_offset_past_the_end_is_empty_not_an_error(self) -> None:
        aid = await self.store.put(self.tid("past"), "t", "short")
        got = await self.store.get(aid, offset=500, length=10)
        self.assertEqual(got.content, "")
        self.assertEqual(got.total_chars, 5)

    async def test_negative_offset_clamps_to_zero(self) -> None:
        aid = await self.store.put(self.tid("neg"), "t", "abcdef")
        self.assertEqual((await self.store.get(aid, offset=-5, length=3)).content, "abc")

    async def test_negative_length_reads_the_whole_body(self) -> None:
        """``length=-1`` is the internal whole-body read ``grep`` relies on."""
        aid = await self.store.put(self.tid("whole"), "t", "abcdef")
        self.assertEqual((await self.store.get(aid, offset=0, length=-1)).content, "abcdef")

    async def test_empty_content_roundtrips(self) -> None:
        aid = await self.store.put(self.tid("empty"), "t", "")
        got = await self.store.get(aid)
        self.assertEqual(got.content, "")
        self.assertEqual(got.total_chars, 0)

    async def test_unicode_survives(self) -> None:
        body = "héllo — 世界 🎉"
        aid = await self.store.put(self.tid("uni"), "t", body)
        self.assertEqual((await self.store.get(aid)).content, body)

    async def test_grep_finds_matching_lines(self) -> None:
        aid = await self.store.put(
            self.tid("grep"), "t", "alpha\nbeta\ngamma beta\ndelta")
        hits = await self.store.grep(aid, r"beta")
        self.assertEqual(hits, ["2: beta", "3: gamma beta"])

    async def test_grep_on_a_missing_artifact_is_none(self) -> None:
        self.assertIsNone(await self.store.grep("art_nope", "x"))

    async def test_grep_with_no_match_is_empty(self) -> None:
        aid = await self.store.put(self.tid("nogrep"), "t", "alpha\nbeta")
        self.assertEqual(await self.store.grep(aid, r"zzz"), [])

    async def test_grep_reports_an_invalid_regex(self) -> None:
        aid = await self.store.put(self.tid("badre"), "t", "alpha")
        out = await self.store.grep(aid, "[unclosed")
        self.assertEqual(len(out), 1)
        self.assertIn("invalid regex", out[0])

    async def test_created_ts_comes_from_the_application(self) -> None:
        """One clock — ``delete_older_than`` compares it to an app-side cutoff."""
        before = time.time()
        aid = await self.store.put(self.tid("clock"), "t", "x")
        after = time.time()
        ts = await self.created_ts_of(aid)
        self.assertGreaterEqual(ts, before - 5)
        self.assertLessEqual(ts, after + 5)

    async def test_delete_older_than_respects_the_cutoff(self) -> None:
        aid = await self.store.put(self.tid("ttl"), "t", "keep me")
        await self.store.delete_older_than(time.time() - 3600)
        self.assertIsNotNone(
            await self.store.get(aid),
            "the TTL sweep deleted an artifact newer than the cutoff",
        )
        await self.store.delete_older_than(time.time() + 3600)
        self.assertIsNone(await self.store.get(aid))

    async def test_recall_degrades_rather_than_raising(self) -> None:
        """A caller that skipped ``supports_recall`` must not crash."""
        aid = await self.store.put(self.tid("recall"), "t", "some content")
        self.assertIsNotNone(aid)
        hits = await self.store.recall(self.tid("recall"), "content", k=3)
        self.assertIsInstance(hits, list)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedArtifacts(_ArtifactContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.context.artifacts_unified import sqlite_artifact_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_artifact_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def tid(self, name: str) -> str:
        return f"thread-{name}"

    async def created_ts_of(self, artifact_id: str) -> float:
        async with self.store._d.reading() as conn:
            cur = await conn.execute(
                "SELECT created_ts FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            return float((await cur.fetchone())[0])

    async def test_recall_is_unavailable(self) -> None:
        self.assertFalse(self.store.supports_recall)
        self.assertEqual(await self.store.recall(self.tid("x"), "q"), [])

    async def test_thread_needs_no_parent_row(self) -> None:
        """SQLite has no artifacts_thread_fk, so ensure_parent must no-op."""
        aid = await self.store.put("thread-never-created", "t", "body")
        self.assertIsNotNone(await self.store.get(aid))


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedArtifacts(_ArtifactContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.context.artifacts_unified import pg_artifact_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        # semantic_recall off: these cases cover storage, not the vector index.
        self.store = pg_artifact_store(self.pool)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM artifact_chunks WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",))
            await conn.execute(
                "DELETE FROM artifacts WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",))
            await conn.execute(
                "DELETE FROM threads WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",))
        await self.pool.close()

    def tid(self, name: str) -> str:
        return f"thread-{name}-{self._suffix}"

    async def created_ts_of(self, artifact_id: str) -> float:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT extract(epoch FROM created_ts) AS ts FROM artifacts "
                "WHERE artifact_id = %s", (artifact_id,))
            return float((await cur.fetchone())[0])

    async def test_parent_thread_row_is_created(self) -> None:
        """``artifacts_thread_fk`` needs the hub row; ensure_parent supplies it."""
        tid = self.tid("fk")
        await self.store.put(tid, "t", "body")
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM threads WHERE thread_id = %s", (tid,))
            self.assertEqual((await cur.fetchone())[0], 1)

    async def test_recall_is_off_without_semantic_recall(self) -> None:
        """The flag gates it, not merely the presence of a pool."""
        self.assertFalse(self.store.supports_recall)


class RecallCapabilityIsDeclared(unittest.TestCase):
    """``supports_recall`` is the pattern the review holds up as correct."""

    def test_it_is_a_property_on_the_class(self) -> None:
        from yuyutsava.context.artifacts_unified import UnifiedArtifactStore

        self.assertIsInstance(
            UnifiedArtifactStore.supports_recall, property,
            "supports_recall stopped being a declared property — bootstrap.py "
            "branches on it, and a getattr probe is what ADR-002 removed "
            "everywhere else",
        )

    def test_recall_requires_pool_embedder_and_flag(self) -> None:
        """All three, so a half-configured store reports False rather than failing late."""
        from yuyutsava.context.artifacts_unified import UnifiedArtifactStore
        from yuyutsava.storage.dialect import PostgresDialect

        class _Embedder:
            async def embed(self, *a, **k): return []

        pool = object()
        d = PostgresDialect(pool)
        self.assertFalse(UnifiedArtifactStore(d, pool=pool).supports_recall)
        self.assertFalse(
            UnifiedArtifactStore(d, pool=pool, embedder=_Embedder()).supports_recall)
        self.assertFalse(
            UnifiedArtifactStore(d, embedder=_Embedder(), semantic_recall=True).supports_recall)
        self.assertTrue(
            UnifiedArtifactStore(
                d, pool=pool, embedder=_Embedder(), semantic_recall=True
            ).supports_recall)

    def test_sqlite_never_reports_recall(self) -> None:
        """Even handed an embedder and the flag — there is no vector column."""
        import tempfile as _tf

        from yuyutsava.context.artifacts_unified import ArtifactSchema, UnifiedArtifactStore
        from yuyutsava.storage.dialect import SqliteDialect

        class _Embedder:
            async def embed(self, *a, **k): return []

        with _tf.TemporaryDirectory() as tmp:
            store = UnifiedArtifactStore(
                SqliteDialect(ArtifactSchema(Path(tmp) / "state.db")),
                pool=object(), embedder=_Embedder(), semantic_recall=True,
            )
            self.assertFalse(
                store.supports_recall,
                "a SQLite store claimed recall; every put would spawn an "
                "indexing task writing to a table that does not exist",
            )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
