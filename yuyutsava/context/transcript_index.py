"""Per-conversation semantic recall over the verbatim transcript (Postgres).

The agent's working memory is the LangGraph checkpoint, which the
:class:`~yuyutsava.storage.sweeper.UnifiedSweeper` deletes ~1h after a thread is
minted. The verbatim transcript (``transcript_messages`` / ``voice_messages``)
lives ~7 days, so a resumed session *displays* history the agent no longer
remembers. This index closes that gap: it embeds each human/assistant turn into
``transcript_chunks`` (migration v13) and lets a per-turn injector recall the
chunks relevant to the user's latest message, scoped to *this* thread — so the
agent recalls prior topics even after its checkpoint is gone.

Two entry points feed the index, both idempotent via ``source_id``:

* :meth:`index_messages` — live write-through from
  :class:`~yuyutsava.context.transcript_policy.TranscriptRecorderPolicy`
  as new turns run (covers text *and* voice — both flow through the bundle).
* :meth:`backfill_thread` — reads a thread's existing rows from the durable
  ``transcript_messages`` + ``voice_messages`` tables on first touch, so a
  session that predates this feature (or whose checkpoint was swept) is still
  recallable.

Retrieval implements :class:`~yuyutsava.retrieval.store.VectorStore` so the
generic :class:`~yuyutsava.retrieval.injector.RetrievalInjector` can drive it.
Everything is best-effort and never raises into a turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ulid import ULID

from yuyutsava.retrieval.chunking import chunk_text
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.store import VectorStore
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.transcript_index")

# ``role`` is projected into Hit.payload so the injector can label the speaker.
_TRANSCRIPT_CHUNKS_TABLE = PgVectorTable(
    table="transcript_chunks",
    id_col="chunk_id",
    text_col="text",
    extra_cols=("role",),
)


def _mint_chunk_id() -> str:
    return f"tch_{ULID()}"


def _flatten_content(content: object) -> str:
    """Flatten a LangChain message ``content`` (str or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


class PgTranscriptIndex(VectorStore):
    """pgvector recall over a thread's conversation turns."""

    def __init__(
        self,
        pool: PgPool,
        *,
        embedder: Any,
        chunk_chars: int = 1_200,
        min_score: float = 0.0,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._chunk_chars = chunk_chars
        self._search = PgVectorSearch(pool, _TRANSCRIPT_CHUNKS_TABLE, min_score=min_score)
        self._tasks: set[asyncio.Task] = set()
        # Threads already backfilled this process — one durable read per thread.
        self._backfilled: set[str] = set()
        self._backfill_locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self._embedder is not None

    # ------------------------------------------------------------------ #
    # Write path                                                          #
    # ------------------------------------------------------------------ #

    def index_messages(self, thread_id: str, messages: Sequence[Any]) -> None:
        """Fire-and-forget index of new human/assistant turns. Never blocks."""
        if not self.enabled or not thread_id:
            return
        items = _messages_to_items(thread_id, messages)
        if not items:
            return
        self._spawn(self._index_items(thread_id, items))

    async def ensure_backfilled(self, thread_id: str) -> None:
        """Backfill a thread's durable history once per process (awaitable).

        Awaited by the injector on the first turn after a resume so that turn
        already recalls prior history, rather than only later turns. Guarded per
        thread so concurrent turns don't double-read. Never raises.
        """
        if not self.enabled or not thread_id or thread_id in self._backfilled:
            return
        lock = self._backfill_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            if thread_id in self._backfilled:
                return
            await self.backfill_thread(thread_id)
            self._backfilled.add(thread_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def backfill_thread(self, thread_id: str) -> int:
        """Index a thread's existing transcript + voice rows. Returns items indexed."""
        try:
            items = await self._read_durable_history(thread_id)
            if not items:
                return 0
            return await self._index_items(thread_id, items)
        except Exception:
            logger.warning("transcript_index: backfill of %s failed", thread_id, exc_info=True)
            return 0

    async def _read_durable_history(self, thread_id: str) -> list[tuple[str, str, str]]:
        """``[(source_id, role, text), …]`` from transcript_messages + voice_messages."""
        items: list[tuple[str, str, str]] = []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT message_id, type, content FROM transcript_messages "
                "WHERE thread_id = %s ORDER BY seq ASC",
                (thread_id,),
            )
            for message_id, mtype, content in await cur.fetchall():
                if mtype not in ("human", "ai"):
                    continue
                data = content.get("data", {}) if isinstance(content, dict) else {}
                if isinstance(content, str):  # some drivers hand back text
                    try:
                        data = json.loads(content).get("data", {})
                    except Exception:
                        data = {}
                text = _flatten_content(data.get("content", "")).strip()
                if not text:
                    continue
                role = "user" if mtype == "human" else "assistant"
                items.append((str(message_id), role, text))

            cur = await conn.execute(
                "SELECT seq, role, text FROM voice_messages "
                "WHERE thread_id = %s ORDER BY seq ASC",
                (thread_id,),
            )
            for seq, role, text in await cur.fetchall():
                text = (text or "").strip()
                if not text:
                    continue
                items.append((f"{thread_id}:{seq}", str(role or "user"), text))
        return items

    async def _index_items(self, thread_id: str, items: list[tuple[str, str, str]]) -> int:
        """Chunk + embed + insert, skipping already-indexed source_ids. Never raises."""
        try:
            source_ids = [sid for sid, _, _ in items]
            existing = await self._existing_source_ids(thread_id, source_ids)
            todo = [(sid, role, text) for sid, role, text in items if sid not in existing]
            if not todo:
                return 0

            rows: list[tuple[str, str, str, int, str]] = []  # (source_id, role, ...) per chunk
            texts: list[str] = []
            for source_id, role, text in todo:
                for chunk in chunk_text(text, target_chars=self._chunk_chars):
                    rows.append((source_id, role, chunk.seq, chunk.text))
                    texts.append(chunk.text)
            if not rows:
                return 0

            try:
                embedded = await self._embedder.embed(texts, mode="document")
                vectors: list[str | None] = [vector_literal(v) for v in embedded]
            except Exception:
                logger.warning(
                    "transcript_index: embedding failed for %s — storing %d NULL rows",
                    thread_id, len(rows), exc_info=True,
                )
                vectors = [None] * len(rows)

            async with self._pool.connection() as conn:
                await ensure_thread(conn, thread_id)  # satisfy transcript_chunks_thread_fk
                for (source_id, role, seq, text), vec in zip(rows, vectors):
                    await conn.execute(
                        "INSERT INTO transcript_chunks "
                        "(chunk_id, thread_id, source_id, role, seq, text, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                        (_mint_chunk_id(), thread_id, source_id, role, seq, text, vec),
                    )
            return len(todo)
        except Exception:
            logger.warning("transcript_index: indexing %s failed", thread_id, exc_info=True)
            return 0

    async def _existing_source_ids(self, thread_id: str, source_ids: list[str]) -> set[str]:
        if not source_ids:
            return set()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT source_id FROM transcript_chunks "
                "WHERE thread_id = %s AND source_id = ANY(%s)",
                (thread_id, source_ids),
            )
            return {r[0] for r in await cur.fetchall()}

    # ------------------------------------------------------------------ #
    # Read path (VectorStore)                                             #
    # ------------------------------------------------------------------ #

    async def search(
        self, query: str, k: int = 5, filters: Mapping[str, Any] | None = None
    ) -> list[Hit]:
        """Top-k relevant turns for ``query``, scoped to ``filters['thread_id']``.

        Falls back to keyword search when the embedder is unavailable. Returns []
        (never raises) so a retrieval hiccup can't break a turn.
        """
        thread_id = (filters or {}).get("thread_id") if filters else None
        where = "AND thread_id = %s" if thread_id else ""
        params = [thread_id] if thread_id else []
        try:
            qvec = vector_literal(await self._embedder.embed_one(query, mode="query"))
        except Exception:
            logger.warning("transcript_index: query embed failed — keyword fallback", exc_info=True)
            try:
                async with self._pool.connection() as conn:
                    return await self._search.keyword_search(conn, query, k, where=where, params=params)
            except Exception:
                logger.warning("transcript_index: keyword search failed", exc_info=True)
                return []
        try:
            async with self._pool.connection() as conn:
                return await self._search.vector_search(conn, qvec, k, where=where, params=params)
        except Exception:
            logger.warning("transcript_index: vector search failed", exc_info=True)
            return []


def _messages_to_items(thread_id: str, messages: Sequence[Any]) -> list[tuple[str, str, str]]:
    """``[(source_id, role, text), …]`` for human/AI messages with prose + an id."""
    items: list[tuple[str, str, str]] = []
    for m in messages:
        mtype = getattr(m, "type", "")
        if mtype not in ("human", "ai"):
            continue
        source_id = getattr(m, "id", None)
        if not source_id:
            continue
        text = _flatten_content(getattr(m, "content", "")).strip()
        if not text:
            continue  # tool-call-only AI turns carry no prose
        role = "user" if mtype == "human" else "assistant"
        items.append((str(source_id), role, text))
    return items
