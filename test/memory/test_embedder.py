"""Unit tests for the embeddings client: nomic prefixes, char cap, dim check.

The fake HTTP client goes in through ``client_factory`` — the seam ``Embedder``
actually uses — and every test asserts the fake received the request. The fake
used to be assigned to an attribute nothing read, so each test quietly posted to
the real embedding endpoint (R-TST-08). The settings point at a closed port, so
a request that escaped the fake would fail rather than find a local server.

Run:  uv run python -m unittest test.memory.test_embedder -v
"""

from __future__ import annotations

import unittest

from yuyutsava.memory.config import EMBEDDING_DIM, MemorySettings
from yuyutsava.memory.embedder import _MAX_EMBED_CHARS, Embedder

NOWHERE = "http://127.0.0.1:9"


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records each request and returns one canned vector per input."""

    def __init__(self, dim: int = EMBEDDING_DIM, *, fail: bool = False) -> None:
        self.requests: list[dict] = []
        self._dim = dim
        self._fail = fail

    async def post(self, url: str, json: dict) -> _FakeResponse:
        self.requests.append(json)
        if self._fail:
            raise RuntimeError("connection refused")
        n = len(json["input"])
        data = [{"index": i, "embedding": [0.0] * self._dim} for i in range(n)]
        return _FakeResponse({"data": data})

    @property
    def last_input(self) -> list[str]:
        return self.requests[-1]["input"]


def _embedder(
    *, use_prefixes: bool = True, dim: int = EMBEDDING_DIM, fail: bool = False
) -> tuple[Embedder, _FakeClient]:
    fake = _FakeClient(dim=dim, fail=fail)
    settings = MemorySettings(embed_use_prefixes=use_prefixes, embed_base_url=NOWHERE)
    return Embedder(settings, client_factory=lambda: fake), fake  # type: ignore[arg-type,return-value]


class EmbedderTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_prefix_applied(self) -> None:
        emb, fake = _embedder(use_prefixes=True)
        await emb.embed_one("hello world", mode="query")
        self.assertEqual(fake.last_input, ["search_query: hello world"])

    async def test_document_prefix_applied(self) -> None:
        emb, fake = _embedder(use_prefixes=True)
        await emb.embed_one("a durable fact", mode="document")
        self.assertEqual(fake.last_input, ["search_document: a durable fact"])

    async def test_no_prefix_when_disabled(self) -> None:
        emb, fake = _embedder(use_prefixes=False)
        await emb.embed_one("raw text", mode="query")
        self.assertEqual(fake.last_input, ["raw text"])

    async def test_input_capped(self) -> None:
        emb, fake = _embedder(use_prefixes=False)
        await emb.embed_one("x" * (_MAX_EMBED_CHARS + 500), mode="document")
        self.assertEqual(len(fake.last_input[0]), _MAX_EMBED_CHARS)

    async def test_dim_mismatch_raises(self) -> None:
        emb, fake = _embedder(use_prefixes=False, dim=EMBEDDING_DIM - 1)
        with self.assertRaises(ValueError):
            await emb.embed_one("x")
        self.assertEqual(len(fake.requests), 1)

    async def test_healthcheck_false_on_error(self) -> None:
        emb, fake = _embedder(fail=True)
        self.assertFalse(await emb.healthcheck())
        self.assertEqual(len(fake.requests), 1)

    async def test_healthcheck_true_when_reachable(self) -> None:
        emb, fake = _embedder()
        self.assertTrue(await emb.healthcheck())
        self.assertEqual(len(fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
