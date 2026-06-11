"""Thin async wrapper over an OpenAI-compatible ``/embeddings`` endpoint.

Works against Ollama (default, local) or any hosted provider. Failures are
the caller's problem to swallow — memory writes must never fail an agent
turn, so callers wrap in try/except (see compaction middleware / store).
"""

from __future__ import annotations

import logging

import httpx

from yuyutsava.memory.config import EMBEDDING_DIM, MemorySettings

logger = logging.getLogger("yuyutsava.memory.embedder")


class Embedder:
    """Async embeddings client; one instance shared per process."""

    def __init__(self, settings: MemorySettings, *, timeout_sec: float = 30.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.embed_base_url,
            headers={"Authorization": f"Bearer {settings.embed_api_key}"},
            timeout=timeout_sec,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``; raises on HTTP errors or dimension mismatch."""
        if not texts:
            return []
        resp = await self._client.post(
            "/embeddings",
            json={"model": self._settings.embed_model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        vectors = [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]
        if vectors and len(vectors[0]) != EMBEDDING_DIM:
            raise ValueError(
                f"embedder returned dim {len(vectors[0])}, expected {EMBEDDING_DIM} "
                f"(model {self._settings.embed_model!r} doesn't match the "
                "memories.embedding column — see storage/pg/migrations.py)"
            )
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]

    async def aclose(self) -> None:
        await self._client.aclose()
