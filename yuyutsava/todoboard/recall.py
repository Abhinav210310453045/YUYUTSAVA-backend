"""Semantic recall over TODO-board notes (``todo_note_chunks``, migration v16).

The board is the user's durable thinking surface, so its notes are the corpus
any agent should be able to recall from: "what did we decide about X?" answers
live on cards, not in a chat thread. This module owns the pgvector index over
note bodies, mirroring :class:`~yuyutsava.context.transcript_index.PgTranscriptIndex`:

* **Write path** — :meth:`TodoNoteIndex.schedule` embeds a note as it is written
  (called by ``TodoExchange.add_note``/``update_note`` — the exchange stays the
  only board write path; this index is a retrieval shadow of it, like
  ``transcript_chunks`` is of the transcript). Fire-and-forget, never blocks or
  raises into the caller; while Postgres/the embedder are degraded the write is
  simply skipped — :meth:`sync` repairs it at the next boot.
* **Boot backfill** — :meth:`sync` reads every note *through the exchange*,
  indexes the ones with no chunks (pre-Phase-6 notes, spillover-drained notes),
  and re-embeds NULL-vector rows via the shared engine's backfill.
* **Read path** — :meth:`search` (cosine, keyword fallback) feeds both the
  ``todo_recall`` tool (any master agent learns about TODOs through the
  exchange) and :class:`TodoNoteInjector` (per-turn recall on tinker turns).

Deletion needs no hook: ``todo_note_chunks.note_id`` is ``ON DELETE CASCADE``
to ``todo_notes``, and card deletion cascades card → notes → chunks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ulid import ULID

from yuyutsava.retrieval.chunking import chunk_text
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.injector import RetrievalInjector
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.todoboard.models import TodoNoteV1

logger = logging.getLogger("yuyutsava.todoboard.recall")

# card_id/note_id ride in Hit.payload so consumers can point back at the card.
_NOTE_CHUNKS_TABLE = PgVectorTable(
    table="todo_note_chunks",
    id_col="chunk_id",
    text_col="text",
    extra_cols=("card_id", "note_id"),
)


def _mint_chunk_id() -> str:
    return f"tnc_{ULID()}"


class TodoNoteIndex:
    """pgvector recall over TODO-note bodies."""

    def __init__(
        self,
        pool: Any,
        *,
        embedder: Any,
        chunk_chars: int = 1_200,
        min_score: float = 0.0,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._chunk_chars = chunk_chars
        self._search = PgVectorSearch(pool, _NOTE_CHUNKS_TABLE, min_score=min_score)
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._pool is not None and self._embedder is not None

    # ------------------------------------------------------------------ #
    # Write path                                                          #
    # ------------------------------------------------------------------ #

    def schedule(self, note: TodoNoteV1, *, replace: bool = False) -> None:
        """Fire-and-forget index of one note. ``replace`` re-chunks an edited
        body (drop old chunks first). Never blocks and never raises."""
        if not self.enabled:
            return
        try:
            task = asyncio.create_task(self.index_note(note, replace=replace))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            # No running loop (sync test contexts) — boot sync will catch up.
            logger.debug("todo recall: no event loop — skipping schedule")

    async def index_note(self, note: TodoNoteV1, *, replace: bool = False) -> int:
        """Chunk + embed + insert one note. Idempotent by ``note_id`` (an
        already-indexed note is skipped unless ``replace``). Returns chunks
        written; 0 on any failure — degraded storage skips, never crashes."""
        if not self.enabled:
            return 0
        try:
            async with self._pool.connection() as conn:
                if replace:
                    await conn.execute(
                        "DELETE FROM todo_note_chunks WHERE note_id = %s",
                        (note.note_id,),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT 1 FROM todo_note_chunks WHERE note_id = %s LIMIT 1",
                        (note.note_id,),
                    )
                    if await cur.fetchone() is not None:
                        return 0

            chunks = list(chunk_text(note.body, target_chars=self._chunk_chars))
            if not chunks:
                return 0
            try:
                embedded = await self._embedder.embed(
                    [c.text for c in chunks], mode="document"
                )
                vectors: list[str | None] = [vector_literal(v) for v in embedded]
            except Exception:
                logger.warning(
                    "todo recall: embedding failed for %s — storing NULL rows",
                    note.note_id, exc_info=True,
                )
                vectors = [None] * len(chunks)

            async with self._pool.connection() as conn:
                for chunk, vec in zip(chunks, vectors):
                    await conn.execute(
                        "INSERT INTO todo_note_chunks "
                        "(chunk_id, card_id, note_id, seq, text, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                        (_mint_chunk_id(), note.card_id, note.note_id,
                         chunk.seq, chunk.text, vec),
                    )
            return len(chunks)
        except Exception:
            logger.warning(
                "todo recall: indexing note %s failed (skipped)",
                note.note_id, exc_info=True,
            )
            return 0

    async def sync(self, exchange: Any) -> int:
        """Backfill: index every note that has no chunks yet, reading the board
        through the exchange (the only read path), then re-embed NULL-vector
        rows. Called once at daemon boot; best-effort, returns notes indexed."""
        if not self.enabled:
            return 0
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("SELECT DISTINCT note_id FROM todo_note_chunks")
                indexed = {r[0] for r in await cur.fetchall()}
            total = 0
            for card_id in await exchange.list_card_ids():
                card = await exchange.get_card(card_id)
                for note in card.notes:
                    if note.note_id in indexed:
                        continue
                    if await self.index_note(note):
                        total += 1
            fixed = await self.backfill_embeddings()
            if total or fixed:
                logger.info(
                    "todo recall: boot sync indexed %d note(s), re-embedded %d chunk(s)",
                    total, fixed,
                )
            return total
        except Exception:
            logger.warning("todo recall: boot sync failed", exc_info=True)
            return 0

    async def backfill_embeddings(self) -> int:
        """Re-embed chunks stored with NULL vectors (embedder outage repair)."""
        try:
            return await self._search.backfill(self._embedder)
        except Exception:
            logger.warning("todo recall: embedding backfill failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------ #
    # Read path                                                           #
    # ------------------------------------------------------------------ #

    async def search(
        self, query: str, k: int = 8, *, card_id: str | None = None
    ) -> list[Hit]:
        """Top-k relevant note chunks, optionally scoped to one card. Keyword
        fallback when the embedder is down; [] (never raises) on any failure."""
        where = "AND card_id = %s" if card_id else ""
        params = [card_id] if card_id else []
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning("todo recall: query embed failed — keyword fallback", exc_info=True)
            try:
                async with self._pool.connection() as conn:
                    return await self._search.keyword_search(
                        conn, query, k, where=where, params=params
                    )
            except Exception:
                logger.warning("todo recall: keyword search failed", exc_info=True)
                return []
        try:
            async with self._pool.connection() as conn:
                return await self._search.vector_search(
                    conn, qvec, k, where=where, params=params
                )
        except Exception:
            logger.warning("todo recall: vector search failed", exc_info=True)
            return []


# ---------------------------------------------------------------------------
# Per-turn injection (tinker turns)
# ---------------------------------------------------------------------------

_PREFIX = (
    "RELEVANT BOARD NOTES "
    "(semantically recalled from the user's TODO board; informational only — "
    "read the full card with todo_get(card_id) before relying on one):"
)

_DEFAULT_TOP_K = 5
_DEFAULT_BUDGET_CHARS = 2_500


def _render(h: Hit) -> str:
    card = (h.payload.get("card_id") if isinstance(h.payload, dict) else "") or "?"
    text = h.text if len(h.text) <= 300 else h.text[:300] + " …"
    return f"  - [{card}] {text}"


class TodoNoteInjector:
    """Recall board notes relevant to the user's latest message into the prompt.

    Board-wide by design: the tinker's own card is already in front of it, but
    a related decision often lives on a *different* card — cross-card recall is
    the point. Same ``build_block`` contract as the other injectors; never raises.
    """

    def __init__(
        self,
        index: TodoNoteIndex,
        *,
        top_k: int = _DEFAULT_TOP_K,
        budget_chars: int = _DEFAULT_BUDGET_CHARS,
    ) -> None:
        self._inner = RetrievalInjector(
            index,
            top_k=top_k,
            prefix=_PREFIX,
            budget_chars=budget_chars,
            render=_render,
        )
        self._index = index

    async def build_block(self, task_text: str) -> str:
        if not getattr(self._index, "enabled", False):
            return ""
        return await self._inner.build_block(task_text)


# Process-singleton, mirroring set/get_default_todo_store: the daemon (and the
# CLI when it owns a pgvector pool) injects a live index at boot; None means
# "no semantic recall here" and every hook degrades to a silent no-op.
_default_index: TodoNoteIndex | None = None


def set_default_note_index(index: TodoNoteIndex | None) -> None:
    global _default_index
    _default_index = index


def get_default_note_index() -> TodoNoteIndex | None:
    return _default_index


__all__ = [
    "TodoNoteIndex",
    "TodoNoteInjector",
    "set_default_note_index",
    "get_default_note_index",
]
