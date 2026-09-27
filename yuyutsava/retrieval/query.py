"""Embedding for the semantic stores — or saying why not, and degrading.

Every semantic store (memory, skills, artifacts, transcripts, board notes) embeds
the same two ways: a *query*, falling back to keyword search when it cannot, and
the *documents* it writes, storing NULL vectors that a later backfill repairs.
An embedder that is down or unreachable is an expected state that both
fallbacks already handle, so it gets one warning line; anything else is
unexpected and keeps its traceback (R-BUG-13 — handled failures printed full
tracebacks at the end of every turn, where a real crash would appear).
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


async def document_vectors(
    embedder: Any, texts: list[str], *, what: str, log: logging.Logger
) -> list[str | None]:
    """One vector literal per text, or ``None`` for each when embedding failed.

    The rows are written either way: a NULL vector is still found by keyword
    and is re-embedded by the store's backfill once the embedder recovers.
    """
    try:
        return [vector_literal(v) for v in await embedder.embed(texts, mode="document")]
    except httpx.TransportError as exc:
        log.warning(
            "%s: embedder unreachable (%s) — storing without vectors", what, type(exc).__name__
        )
    except Exception:  # noqa: BLE001 — the rows are kept; the traceback says what broke
        log.warning("%s: embedding failed — storing without vectors", what, exc_info=True)
    return [None] * len(texts)
