"""Thin async wrapper over an OpenAI-compatible ``/embeddings`` endpoint.

Works against Ollama (default, local) or any hosted provider. Failures are
the caller's problem to swallow — memory writes must never fail an agent
turn, so callers wrap in try/except (see compaction middleware / store).
"""

from __future__ import annotations

import logging

import httpx

from yuyutsava.aio import LoopLocal
from yuyutsava.memory.config import EMBEDDING_DIM, MemorySettings

logger = logging.getLogger("yuyutsava.memory.embedder")

# nomic-embed-text task-instruction prefixes (see MemorySettings.embed_use_prefixes).
_QUERY_PREFIX = "search_query: "
_DOCUMENT_PREFIX = "search_document: "

# Cap embed input so a long summary can't silently overflow the model context
# (nomic-embed-text is ~2048 tokens ≈ a few thousand chars). We truncate
# explicitly to a known budget rather than letting the server drop the tail.
# Only the vector input is capped; the full text is still stored by the caller.
_MAX_EMBED_CHARS = 6000


class Embedder:
    """Async embeddings client; one instance shared per process.

    One ``httpx.AsyncClient`` per event loop: the stores holding this embedder
    are awaited from both the main loop and the AsyncSubagentHost's uvicorn
    loop, and an ``AsyncClient`` is loop-affine (its transport and anyio
    primitives bind to the loop that first uses it). See Architecture.md
    "Event-loop ownership".
    """

    def __init__(self, settings: MemorySettings, *, timeout_sec: float = 30.0) -> None:
        self._settings = settings
        self._use_prefixes = settings.embed_use_prefixes
        self._clients: LoopLocal[httpx.AsyncClient] = LoopLocal(
            lambda: httpx.AsyncClient(
                base_url=settings.embed_base_url,
                headers={"Authorization": f"Bearer {settings.embed_api_key}"},
                timeout=timeout_sec,
            )
        )

    async def embed(
        self, texts: list[str], *, mode: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts``; raises on HTTP errors or dimension mismatch.

        ``mode`` is ``"document"`` (stored memories) or ``"query"`` (search
        terms); it selects the nomic task prefix when prefixes are enabled.
        Store and query sides MUST pass the matching mode for asymmetric
        retrieval to work.
        """
        if not texts:
            return []
        if any(len(t) > _MAX_EMBED_CHARS for t in texts):
            logger.debug("memory: truncating embed input(s) to %d chars", _MAX_EMBED_CHARS)
            texts = [t[:_MAX_EMBED_CHARS] for t in texts]
        payload_texts = texts
        if self._use_prefixes:
            prefix = _QUERY_PREFIX if mode == "query" else _DOCUMENT_PREFIX
            payload_texts = [prefix + t for t in texts]
        resp = await self._clients.get().post(
            "/embeddings",
            json={"model": self._settings.embed_model, "input": payload_texts},
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

    async def embed_one(self, text: str, *, mode: str = "document") -> list[float]:
        vectors = await self.embed([text], mode=mode)
        return vectors[0]

    async def healthcheck(self) -> bool:
        """True if the endpoint is reachable and returns usable vectors.

        Validates the whole path (connection + model loaded + correct dims) by
        embedding a tiny probe. On failure logs ONE concise warning — no
        traceback — so a down embedder surfaces clearly at startup instead of
        as a storm of per-operation tracebacks later.
        """
        try:
            await self.embed_one("ping", mode="query")
            return True
        except Exception as exc:
            logger.warning(
                "memory: embedder unreachable at %s (model %s): %s — "
                "semantic memory degrades to keyword search until it recovers",
                self._settings.embed_base_url, self._settings.embed_model, exc,
            )
            return False

    async def aclose(self) -> None:
        """Close the current loop's client; clients on other loops cannot be
        closed from here (aclose is loop-affine) and die with the process."""
        client = self._clients.pop_current()
        if client is not None:
            await client.aclose()
