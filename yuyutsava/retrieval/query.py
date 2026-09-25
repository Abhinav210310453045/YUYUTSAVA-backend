"""Embedding a search query — or saying why not, and falling back.

Every semantic store (memory, skills, artifacts, transcripts, board notes) embeds
its query the same way and falls back to keyword search when it cannot. An
embedder that is down or unreachable is an expected state, and the fallback
already handles it, so it gets one warning line; anything else is unexpected
and keeps its traceback (R-BUG-13 — handled failures printed full tracebacks at
the end of every turn, where a real crash would appear).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from yuyutsava.retrieval.vector import vector_literal


async def query_vector(embedder: Any, query: str, *, what: str, log: logging.Logger) -> str | None:
    """The query's vector literal, or ``None`` when the caller should fall back."""
    try:
        return vector_literal(await embedder.embed_one(query, mode="query"))
    except httpx.TransportError as exc:
        log.warning("%s: embedder unreachable (%s) — keyword fallback", what, type(exc).__name__)
    except Exception:  # noqa: BLE001 — a fallback exists; the traceback says what broke
        log.warning("%s: query embedding failed — keyword fallback", what, exc_info=True)
    return None
