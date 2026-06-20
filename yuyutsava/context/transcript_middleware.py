"""Recorder middleware: persist every conversation message to the DB.

Appends messages to :class:`~yuyutsava.context.transcript_store.TranscriptStore`
as the agent runs, giving a durable, queryable full transcript independent of
LangGraph checkpoints (swept after ~1h) and compaction (which removes messages
from live state via ``RemoveMessage`` but never from the transcript table).

It records on every lifecycle edge — ``abefore_model`` (catches the leading
human message and prior tool results), ``aafter_model`` (the freshly produced
AI message), and ``aafter_agent`` (anything appended after the last model
call). Each edge persists the full ``state["messages"]``; a per-thread
in-memory ``seen`` set skips ids already written this process, and the store's
``message_id`` dedup (``INSERT OR IGNORE`` / ``ON CONFLICT DO NOTHING``) makes
cross-process resumes idempotent.

Best-effort by design: a DB failure logs and returns ``None`` (no state
change) — it never fails the agent turn, matching the discipline of
``ToolResultOffloadMiddleware`` and ``YuyutsavaCompactionMiddleware``.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage

from yuyutsava.context.transcript_store import TranscriptStore

logger = logging.getLogger("yuyutsava.context.transcript")


def _current_thread_id() -> str:
    """Thread id from the active LangGraph run config, or empty string."""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "")
    except Exception:
        return ""


class TranscriptRecorderMiddleware(AgentMiddleware):
    """Persist conversation messages to the transcript store as they appear."""

    def __init__(self, store: TranscriptStore) -> None:
        super().__init__()
        self._store = store
        # thread_id -> message_ids already persisted this process. Bounds DB
        # writes to genuinely-new messages; the store dedups across processes.
        self._seen: dict[str, set[str]] = {}

    async def _record(self, state: Any) -> None:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        if not messages:
            return
        thread_id = _current_thread_id()
        if not thread_id:
            return
        seen = self._seen.setdefault(thread_id, set())
        fresh: list[BaseMessage] = [
            m for m in messages if getattr(m, "id", None) and m.id not in seen
        ]
        if not fresh:
            return
        try:
            await self._store.put_messages(thread_id, fresh)
        except Exception:
            logger.exception("transcript: failed to persist %d messages", len(fresh))
            return
        seen.update(m.id for m in fresh)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        await self._record(state)
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        await self._record(state)
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        await self._record(state)
        return None
