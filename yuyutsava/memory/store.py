"""Memory store: pgvector-backed semantic search with a SQLite keyword twin.

The retrieval mechanics (cosine search, keyword fallback, dedup probe, backfill)
live in the reusable :mod:`yuyutsava.retrieval` package and are shared with the
skills store. This module keeps only *memory policy*: valid kinds, the
``RELEVANT MEMORY`` write contract, thread linkage, and the public
``MemoryStore``/``MemoryHit`` surface that the rest of the system depends on.

Write contract: ``add`` never raises out to agent code paths *for embedding
failures* — a memory row without an embedding is still keyword-findable and
strictly better than a lost memory. Database failures do raise; callers on
hot paths (compaction, orchestrator loop) wrap in try/except.

Vectors travel as pgvector's text literal (``'[0.1,0.2,…]'::vector``) so no
per-connection type registration is needed through the pool.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from yuyutsava.memory.embedder import Embedder
from yuyutsava.retrieval.keyword import keyword_tokens as _keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal as _vector_literal
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.memory.store")

VALID_KINDS = ("task_outcome", "summary", "fact", "preference")

# Column map for the shared pgvector engine. ``kind`` rides along in the payload.
_MEMORIES_TABLE = PgVectorTable(
    table="memories",
    id_col="memory_id",
    text_col="text",
    extra_cols=("kind",),
)


def mint_memory_id() -> str:
    return f"mem_{ULID()}"


@dataclass(frozen=True)
class MemoryHit:
    memory_id: str
    kind: str
    text: str
    score: float  # cosine similarity in [0,1] on pg; 0.0 on the keyword twin


class MemoryStore(ABC):
    @abstractmethod
    async def add(
        self,
        *,
        kind: str,
        text: str,
        source_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store one memory; returns its id."""

    @abstractmethod
    async def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryHit]:
        """Top-k most relevant memories for ``query``."""


def _kind_filter(kinds: tuple[str, ...] | None) -> tuple[str, list]:
    """Build the ``AND kind = ANY(%s)`` clause + params for the pg engine."""
    if kinds:
        return "AND kind = ANY(%s)", [list(kinds)]
    return "", []



# NOTE: PgMemoryStore was replaced on 2026-08-09 by UnifiedMemoryStore in
# memory/store_unified.py (ADR-002 step 2.5b). The pgvector asymmetry is a
# DECLARED capability (supports_semantic_search), and backfill_embeddings now
# exists on every backend — which is what retired the getattr probes. Parity
# verified on both live backends in test/storage/test_memory_store_parity.py.


# NOTE: SqliteMemoryStore was replaced on 2026-08-09 by UnifiedMemoryStore in
# memory/store_unified.py (ADR-002 step 2.5b). The pgvector asymmetry is a
# DECLARED capability (supports_semantic_search), and backfill_embeddings now
# exists on every backend — which is what retired the getattr probes. Parity
# verified on both live backends in test/storage/test_memory_store_parity.py.
