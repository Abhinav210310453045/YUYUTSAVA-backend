"""Retrieval protocols — semantic search seams, free of pgvector specifics."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VectorSearcher(Protocol):
    """Anything answering "what is relevant to this text?"."""

    async def search(self, query: str, k: int = ..., **kwargs: Any) -> list[Any]: ...


@runtime_checkable
class ConversationIndex(Protocol):
    """Recall of a thread's own swept turns (``PgTranscriptIndex``)."""

    async def search(self, *args: Any, **kwargs: Any) -> list[Any]: ...


__all__ = ["ConversationIndex", "VectorSearcher"]
