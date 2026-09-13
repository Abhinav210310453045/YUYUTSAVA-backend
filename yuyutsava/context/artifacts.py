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

# Hard ceiling on ONE agent-facing read, whatever ``length`` the model asks
# for. This is a losslessness guarantee, not a thrift measure: results over
# ``LIMITS.max_tool_result_chars`` (100k) are replaced wholesale by
# ``guard_tool_result``, and the ctx_* readers are deliberately exempt from
# offload — so an unclamped ``ctx_fetch_artifact(length=200000)`` would come
# back as a "too large" notice with the body unreachable. Clamping instead
# returns a real slice plus a ``[more: … offset=N]`` line, so the agent pages
# to the end rather than hitting a wall. Keep this well under 100k.
MAX_SLICE_CHARS = 40_000

MAX_GREP_MATCHES = 20

# Default window for a line-addressed read (ctx_fetch_artifact(start_line=…)).
DEFAULT_LINE_COUNT = 200


def clamp_slice_length(length: int) -> int:
    """Agent-requested ``length`` → a value that cannot trip the size guard.

    ``length < 0`` is the store's internal "whole body" marker (grep, line
    reads) and is *not* clamped here — only the agent-facing tool layer passes
    user input through this.
    """
    if length <= 0:
        return DEFAULT_SLICE_CHARS
    return min(length, MAX_SLICE_CHARS)

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
class ArtifactLines:
    """One line-addressed read of an artifact."""

    artifact_id: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    #: True when ``content`` was cut at ``MAX_SLICE_CHARS`` before
    #: ``line_count`` lines were reached — ``end_line`` is then the last line
    #: actually returned, so the next read starts at ``end_line + 1``.
    char_capped: bool = False
    #: True when a SINGLE line was longer than the whole budget and had to be
    #: cut mid-line. Line addressing cannot express "resume inside line N", so
    #: callers must switch to character paging rather than advance to N+1 —
    #: which would silently skip the rest of that line.
    line_truncated: bool = False


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

    async def read_lines(
        self,
        artifact_id: str,
        start_line: int = 1,
        line_count: int = DEFAULT_LINE_COUNT,
    ) -> ArtifactLines | None:
        """Line-addressed read: ``line_count`` lines from ``start_line`` (1-based).

        The companion to :meth:`grep`, which reports ``"<lineno>: <line>"`` —
        this is how the agent turns a match at line 812 into the surrounding
        text without guessing a character offset. Output is still bounded by
        :data:`MAX_SLICE_CHARS`, so a file of very long lines cannot produce a
        result the size guard would suppress.

        ``None`` when the artifact does not exist.
        """
        full = await self.get(artifact_id, offset=0, length=-1)
        if full is None:
            return None
        lines = full.content.splitlines()
        total = len(lines)
        start = max(1, start_line)
        count = max(1, line_count)
        window = lines[start - 1 : start - 1 + count]

        kept: list[str] = []
        used = 0
        capped = False
        truncated = False
        for line in window:
            cost = len(line) + 1
            if used + cost > MAX_SLICE_CHARS:
                if not kept:
                    # One line longer than the entire budget. Return a prefix
                    # rather than a result the size guard would suppress, and
                    # flag it so the caller offers character paging instead of
                    # advancing past the line.
                    kept.append(line[:MAX_SLICE_CHARS])
                    truncated = True
                capped = True
                break
            kept.append(line)
            used += cost

        return ArtifactLines(
            artifact_id=artifact_id,
            content="\n".join(kept),
            start_line=start,
            end_line=start + len(kept) - 1 if kept else start,
            total_lines=total,
            char_capped=capped,
            line_truncated=truncated,
        )


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
