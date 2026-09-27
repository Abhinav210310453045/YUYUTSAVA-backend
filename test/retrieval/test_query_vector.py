"""Embedding that cannot happen degrades — quietly when that is expected.

R-BUG-13: an unreachable embedder made every recall path (memory, skills,
artifacts, transcripts, board notes) print a full traceback at the end of each
turn, though each already fell back to keyword search as designed. The write
side did the same: storing a memory, a skill, a chunk with NULL vectors printed
a traceback for a failure it had handled.
"""

from __future__ import annotations

import asyncio
import logging
import unittest

import httpx

from yuyutsava.retrieval.query import document_vectors, query_vector

LOG = logging.getLogger("test.query_vector")


class _Embedder:
    def __init__(self, result=None, raises=None) -> None:
        self._result, self._raises = result, raises

    async def embed_one(self, text, *, mode):
        if self._raises is not None:
            raise self._raises
        return self._result

    async def embed(self, texts, *, mode):
        if self._raises is not None:
            raise self._raises
        return [self._result for _ in texts]


def _run(embedder):
    return asyncio.run(query_vector(embedder, "q", what="memory", log=LOG))


class QueryVector(unittest.TestCase):
    def test_a_vector_comes_back_as_a_literal(self) -> None:
        self.assertEqual(_run(_Embedder(result=[0.5, 1.0])), "[0.5,1]")

    def test_an_unreachable_embedder_is_one_line(self) -> None:
        with self.assertLogs(LOG, level="WARNING") as logs:
            self.assertIsNone(_run(_Embedder(raises=httpx.ConnectError("refused"))))
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("embedder unreachable (ConnectError)", logs.output[0])

    def test_anything_else_keeps_its_traceback(self) -> None:
        with self.assertLogs(LOG, level="WARNING") as logs:
            self.assertIsNone(_run(_Embedder(raises=ValueError("bad dims"))))
        self.assertIsNotNone(logs.records[0].exc_info)


def _docs(embedder, texts=("a", "b")):
    return asyncio.run(document_vectors(embedder, list(texts), what="memory m1", log=LOG))


class DocumentVectors(unittest.TestCase):
    def test_one_literal_per_text(self) -> None:
        self.assertEqual(_docs(_Embedder(result=[0.5, 1.0])), ["[0.5,1]", "[0.5,1]"])

    def test_an_unreachable_embedder_is_one_line_and_null_rows(self) -> None:
        with self.assertLogs(LOG, level="WARNING") as logs:
            self.assertEqual(_docs(_Embedder(raises=httpx.ConnectError("refused"))), [None, None])
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("memory m1: embedder unreachable (ConnectError)", logs.output[0])

    def test_anything_else_keeps_its_traceback_and_still_null_rows(self) -> None:
        with self.assertLogs(LOG, level="WARNING") as logs:
            self.assertEqual(_docs(_Embedder(raises=ValueError("bad dims"))), [None, None])
        self.assertIsNotNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
