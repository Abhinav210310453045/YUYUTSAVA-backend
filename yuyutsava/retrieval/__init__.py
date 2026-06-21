"""Reusable semantic-retrieval base shared by memory, skills, and future RAG.

The pieces:
  - ``Hit``               — generic search result
  - ``VectorStore``       — the ``search`` contract retrieval consumers depend on
  - ``PgVectorTable`` / ``PgVectorSearch`` — one pgvector engine, parametrized
    over table/column names (cosine, keyword fallback, dedup, backfill)
  - ``RetrievalInjector`` — hard-capped, never-raises prompt block from hits
  - ``vector_literal`` / ``keyword_tokens`` — shared helpers

The embedder lives in :mod:`yuyutsava.memory.embedder` (already generic) and is
shared per-process; it is re-exported here for discoverability.
"""

from __future__ import annotations

from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.injector import RetrievalInjector
from yuyutsava.retrieval.keyword import keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.store import VectorStore
from yuyutsava.retrieval.vector import vector_literal

__all__ = [
    "Hit",
    "RetrievalInjector",
    "VectorStore",
    "PgVectorSearch",
    "PgVectorTable",
    "keyword_tokens",
    "vector_literal",
]
