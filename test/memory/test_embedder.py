"""Unit tests for the embeddings client: nomic prefixes, char cap, dim check.

Run:  uv run python -m unittest test.memory.test_embedder -v
"""

from __future__ import annotations

import unittest

from yuyutsava.memory.config import EMBEDDING_DIM, MemorySettings
from yuyutsava.memory.embedder import _MAX_EMBED_CHARS, Embedder


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records the last request and returns one canned vector per input."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.last_json: dict | None = None
        self._dim = dim

    async def post(self, url: str, json: dict) -> _FakeResponse:
        self.last_json = json
        n = len(json["input"])
        data = [{"index": i, "embedding": [0.0] * self._dim} for i in range(n)]
        return _FakeResponse({"data": data})


def _embedder(*, use_prefixes: bool = True, dim: int = EMBEDDING_DIM) -> Embedder:
    emb = Embedder(MemorySettings(embed_use_prefixes=use_prefixes))
    emb._client = _FakeClient(dim=dim)  # type: ignore[assignment]
    return emb


class EmbedderTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_prefix_applied(self) -> None:
        emb = _embedder(use_prefixes=True)
        await emb.embed_one("hello world", mode="query")
        self.assertEqual(emb._client.last_json["input"], ["search_query: hello world"])

    async def test_document_prefix_applied(self) -> None:
        emb = _embedder(use_prefixes=True)
        await emb.embed_one("a durable fact", mode="document")
        self.assertEqual(
            emb._client.last_json["input"], ["search_document: a durable fact"]
        )

    async def test_no_prefix_when_disabled(self) -> None:
        emb = _embedder(use_prefixes=False)
        await emb.embed_one("raw text", mode="query")
        self.assertEqual(emb._client.last_json["input"], ["raw text"])

    async def test_input_capped(self) -> None:
        emb = _embedder(use_prefixes=False)
        await emb.embed_one("x" * (_MAX_EMBED_CHARS + 500), mode="document")
        self.assertEqual(len(emb._client.last_json["input"][0]), _MAX_EMBED_CHARS)

    async def test_dim_mismatch_raises(self) -> None:
        emb = _embedder(use_prefixes=False, dim=EMBEDDING_DIM - 1)
        with self.assertRaises(ValueError):
            await emb.embed_one("x")

    async def test_healthcheck_false_on_error(self) -> None:
        emb = _embedder()

        async def boom(*_a, **_k):
            raise RuntimeError("connection refused")

        emb._client.post = boom  # type: ignore[assignment]
        self.assertFalse(await emb.healthcheck())

    async def test_healthcheck_true_when_reachable(self) -> None:
        self.assertTrue(await _embedder().healthcheck())


if __name__ == "__main__":
    unittest.main()
