"""The minimal contract a semantic store exposes to retrieval consumers.

Only ``search`` is unified — the write side genuinely differs per domain
(memory takes ``kind``/``source_thread_id``; skills take ``name``/``body``), so
each store keeps its own ``add``/``upsert`` signature. Anything that only needs
to *retrieve* (the injector, future RAG callers) depends on this interface and
nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from yuyutsava.retrieval.hit import Hit


class VectorStore(ABC):
    @abstractmethod
    async def search(
        self, query: str, k: int = 5, filters: Mapping[str, Any] | None = None
    ) -> list[Hit]:
        """Top-k most relevant entries for ``query``."""
