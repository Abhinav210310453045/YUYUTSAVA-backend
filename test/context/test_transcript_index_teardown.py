"""Transcript indexing finishes before the pool closes, and an unreachable embedder
is one line, not a traceback (R-BUG-13).

Every CLI turn ended with two tracebacks it had already handled: the index write
spawned for the turn was still running when the bundle closed the pool (nothing
waited for it), and an unreachable embedder logged a full traceback although the
handler knew exactly what to do — store NULL embeddings.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest

import httpx

from yuyutsava.context.transcript_index import PgTranscriptIndex


class _Conn:
    def __init__(self, log: list) -> None:
        self._log = log

    async def execute(self, sql, params=()):
        self._log.append((sql, params))
        return self

    async def fetchall(self):
        return []


class _Pool:
    def __init__(self) -> None:
        self.log: list = []

    @contextlib.asynccontextmanager
    async def connection(self):
        yield _Conn(self.log)


class _UnreachableEmbedder:
    async def embed(self, texts, *, mode):
        raise httpx.ConnectError("connection refused")


class Drain(unittest.IsolatedAsyncioTestCase):
    async def test_finished_writes_are_awaited_and_stuck_ones_cancelled(self) -> None:
        index = PgTranscriptIndex(_Pool(), embedder=object())
        done: list[str] = []

        async def quick() -> None:
            await asyncio.sleep(0.01)
            done.append("quick")

        stuck_started = asyncio.Event()

        async def stuck() -> None:
            stuck_started.set()
            await asyncio.sleep(60)

        index._spawn(quick())
        index._spawn(stuck())
        await stuck_started.wait()
        await index.drain(timeout=0.2)
        self.assertEqual(done, ["quick"])
        self.assertEqual({t for t in index._tasks if not t.done()}, set())

    async def test_nothing_outstanding_returns_at_once(self) -> None:
        await asyncio.wait_for(PgTranscriptIndex(_Pool(), embedder=object()).drain(), 1)


class UnreachableEmbedder(unittest.IsolatedAsyncioTestCase):
    async def test_one_warning_line_and_null_rows(self) -> None:
        pool = _Pool()
        index = PgTranscriptIndex(pool, embedder=_UnreachableEmbedder())
        with self.assertLogs("yuyutsava.context.transcript_index", level="WARNING") as logs:
            written = await index._index_items("t1", [("m1", "user", "hello there")])
        self.assertEqual(written, 1)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(
            logs.records[0].exc_info, "a handled, expected failure printed a traceback"
        )
        self.assertIn("embedder unreachable", logs.output[0])
        inserts = [p for sql, p in pool.log if sql.startswith("INSERT INTO transcript_chunks")]
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0][-1], "the row should carry a NULL embedding")


if __name__ == "__main__":
    unittest.main()
