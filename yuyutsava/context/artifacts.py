"""Artifact store: full bodies of offloaded tool results.

When :class:`~yuyutsava.context.offload_policy.ToolResultOffloadPolicy`
intercepts an oversized tool result, the complete content lands here and a
digest referencing the ``artifact_id`` takes its place in graph state. The
agent reads slices back via the always-visible ``ctx_fetch_artifact`` /
``ctx_grep_artifact`` tools.

Two interchangeable backends behind :class:`ArtifactStore`:

- :class:`SqliteArtifactStore` — an ``artifacts`` table in ``state.db``
  (own meta table; coexists with the events store via WAL).
- :class:`PgArtifactStore` — the ``artifacts`` table created by
  :mod:`yuyutsava.storage.pg.migrations`.

Retention: artifacts are scratch, not user data. ``delete_older_than`` is
called by :class:`yuyutsava.storage.sweeper.UnifiedSweeper` on its normal
cadence (default TTL 7 days, ``SweeperConfig.artifact_ttl_sec``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from yuyutsava.retrieval.chunking import chunk_text
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.artifacts")

# Default slice served by get() — matches the offload threshold so one fetch
# returns at most one "screenful" of context.
DEFAULT_SLICE_CHARS = 20_000
MAX_GREP_MATCHES = 20

# Column map for the semantic index (migration v12). char_offset lets a recall
# hit map back to ctx_fetch_artifact(offset=…) for the full surrounding body.
_ARTIFACT_CHUNKS_TABLE = PgVectorTable(
    table="artifact_chunks",
    id_col="chunk_id",
    text_col="text",
    extra_cols=("artifact_id", "char_offset"),
)


def thread_id_from_runtime() -> str:
    """Best-effort thread id from the active LangGraph run config."""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "unknown")
    except Exception:
        return "unknown"


def mint_artifact_id() -> str:
    return f"art_{ULID()}"


def mint_chunk_id() -> str:
    return f"ach_{ULID()}"


@dataclass(frozen=True)
class ArtifactSlice:
    """One windowed read of an artifact."""

    artifact_id: str
    content: str
    offset: int
    total_chars: int


@dataclass(frozen=True)
class RecallHit:
    """One semantic hit from the artifact index."""

    artifact_id: str
    char_offset: int
    score: float
    snippet: str


class ArtifactStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        """Store ``content``; return the minted ``artifact_id``."""

    @abstractmethod
    async def get(
        self, artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> ArtifactSlice | None:
        """Windowed read. ``None`` when the artifact does not exist."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """TTL sweep hook. Returns rows deleted."""

    async def grep(
        self, artifact_id: str, pattern: str, max_matches: int = MAX_GREP_MATCHES
    ) -> list[str] | None:
        """Regex search over the artifact's lines: ``["<lineno>: <line>", …]``.

        ``None`` when the artifact does not exist; ``[]`` when nothing matched.
        Shared implementation — both backends fetch then match in-process.
        """
        full = await self.get(artifact_id, offset=0, length=-1)
        if full is None:
            return None
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return [f"invalid regex: {exc}"]
        out: list[str] = []
        for i, line in enumerate(full.content.splitlines(), start=1):
            if rx.search(line):
                out.append(f"{i}: {line[:500]}")
                if len(out) >= max_matches:
                    break
        return out


def _slice(content: str, offset: int, length: int) -> tuple[str, int]:
    total = len(content)
    offset = max(0, offset)
    if length < 0:  # internal "whole body" read for grep
        return content[offset:], total
    return content[offset : offset + max(0, length)], total



# NOTE: SqliteArtifactStore was replaced on 2026-08-09 by UnifiedArtifactStore in
# context/artifacts_unified.py (ADR-002 step 2.5b). `supports_recall` stays a
# declared property — it was already the pattern the review holds up as correct.
# Parity verified on both live backends in test/storage/test_artifact_store_parity.py.


# NOTE: PgArtifactStore was replaced on 2026-08-09 by UnifiedArtifactStore in
# context/artifacts_unified.py (ADR-002 step 2.5b). `supports_recall` stays a
# declared property — it was already the pattern the review holds up as correct.
# Parity verified on both live backends in test/storage/test_artifact_store_parity.py.
