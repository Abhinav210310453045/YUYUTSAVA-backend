"""A search query that cannot be embedded falls back — quietly when that is expected.

R-BUG-13: an unreachable embedder made every recall path (memory, skills,
artifacts, transcripts, board notes) print a full traceback at the end of each
turn, though each already fell back to keyword search as designed.
"""

from __future__ import annotations

import asyncio
import logging
import unittest

import httpx

from yuyutsava.retrieval.query import query_vector

LOG = logging.getLogger("test.query_vector")


class _Embedder:
    def __init__(self, result=None, raises=None) -> None:
        self._result, self._raises = result, raises

    async def embed_one(self, text, *, mode):
        if self._raises is not None:
            raise self._raises
        return self._result


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


if __name__ == "__main__":
    unittest.main()
