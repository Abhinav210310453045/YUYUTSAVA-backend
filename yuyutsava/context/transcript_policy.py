"""Persist conversation messages to the transcript store as they appear.

Phase 4 step 4.8, eleventh migration (was ``TranscriptRecorderMiddleware``).

The checkpointer sweeps old turns, so without this the full conversation is only
ever in memory. Recording on **every** phase — before a model call, after one,
and once the agent finishes — is what makes the transcript complete regardless of
where a run stops: an interrupted turn still has its human message recorded, and
a turn that ends in a tool call still has the tool result.

Writes are bounded to genuinely-new messages by an in-process seen-set; the store
dedups across processes as well, so a restart cannot double-record.

Nothing here fails a turn: a store error is logged and swallowed, and the
optional semantic index is fire-and-forget so indexing never adds latency.
"""

from __future__ import annotations

import logging
from typing import Any

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, Turn
from yuyutsava.ports.storage import TranscriptStore

logger = logging.getLogger("yuyutsava.context.transcript")


class TranscriptRecorderPolicy(Policy):
    """Record new messages to the transcript store at every turn boundary."""

    name = "TranscriptRecorderPolicy"

    def __init__(self, store: TranscriptStore, *, index: Any | None = None) -> None:
        super().__init__()
        self._store = store
        # Optional semantic index (PgTranscriptIndex): each newly-recorded turn is
        # also embedded so a resumed session can recall it after checkpoint sweep.
        self._index = index
        # thread_id -> message_ids already persisted this process. Bounds DB
        # writes to genuinely-new messages; the store dedups across processes.
        self._seen: dict[str, set[str]] = {}

    async def before_model(self, turn: Turn) -> Directive | None:
        await self._record(turn)
        return None

    async def after_model(self, turn: Turn) -> Directive | None:
        await self._record(turn)
        return None

    async def after_agent(self, turn: Turn) -> Directive | None:
        await self._record(turn)
        return None

    async def _record(self, turn: Turn) -> None:
        if not turn.messages or not turn.thread_id:
            return
        seen = self._seen.setdefault(turn.thread_id, set())
        fresh = [m for m in turn.messages
                 if getattr(m, "id", None) and m.id not in seen]
        if not fresh:
            return
        try:
            await self._store.put_messages(turn.thread_id, fresh)
        except Exception:
            logger.exception("transcript: failed to persist %d messages", len(fresh))
            return
        seen.update(m.id for m in fresh)
        if self._index is not None:
            try:
                self._index.index_messages(turn.thread_id, fresh)
            except Exception:
                logger.debug("transcript: semantic index enqueue failed", exc_info=True)


__all__ = ["TranscriptRecorderPolicy"]
