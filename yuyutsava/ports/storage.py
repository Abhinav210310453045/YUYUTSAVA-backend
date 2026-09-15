"""Persistence protocols. Structural — no implementation imports these to inherit."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """Offloaded tool results. Backs the ``ctx_*`` tools and the offload middleware."""

    async def put(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get(self, *args: Any, **kwargs: Any) -> Any: ...
    async def grep(self, *args: Any, **kwargs: Any) -> Any: ...
    async def delete_older_than(self, cutoff_ts: float) -> int: ...


@runtime_checkable
class SummaryStore(Protocol):
    """Per-thread compaction summaries."""

    async def put(self, *args: Any, **kwargs: Any) -> int: ...
    async def latest(self, thread_id: str) -> Any: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Durable user knowledge: facts, preferences, task outcomes."""

    async def add(self, *args: Any, **kwargs: Any) -> str: ...
    async def search(self, *args: Any, **kwargs: Any) -> list[Any]: ...


@runtime_checkable
class TranscriptStore(Protocol):
    """Verbatim conversation history, durable beyond the checkpoint sweep."""

    async def put_messages(self, *args: Any, **kwargs: Any) -> Any: ...
    async def list_messages(self, *args: Any, **kwargs: Any) -> list[Any]: ...
    async def delete_older_than(self, cutoff_ts: float) -> int: ...


__all__ = ["ArtifactStore", "MemoryStore", "SummaryStore", "TranscriptStore", "UsageStore"]


@runtime_checkable
class UsageStore(Protocol):
    """Per-LLM-call cost accounting.

    Implemented in :mod:`yuyutsava.storage.usage`. Agents carry the store and
    :class:`~yuyutsava.policy.builtin.usage.UsagePolicy` writes through it;
    typing both against this port keeps ``agents`` independent of the store
    implementations.
    """

    async def add(self, row: Any) -> None: ...
