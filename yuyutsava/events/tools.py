"""
LangChain tool for subagents to fetch a full event payload by id.

This is the **single affordance** for pulling event details. The orchestrator
never calls it (its prompt explicitly forbids that); only specialised
subagents do, after the orchestrator has dispatched them. This is what keeps
the orchestrator's context bounded — events are referenced by id, not embedded.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.storage.events import Store


def make_fetch_event_tool(store: Store) -> BaseTool:
    """Bind the store to a fresh ``fetch_event`` tool. Per-subagent binding."""

    @tool
    async def fetch_event(event_id: str) -> str:
        """Fetch the full payload for an event by its event_id.

        Returns a JSON string with keys: ``topic``, ``ts``, ``payload`` (full
        event details — file paths, metadata, etc.), and optional
        ``blob_path`` if a binary blob (image, audio) is associated.

        Call this **once** per task to get the details you need; do not
        repeatedly poll.
        """
        rec = await store.get_event_payload(event_id)
        if rec is None:
            return json.dumps({"error": "event_not_found", "event_id": event_id})
        return json.dumps(
            {
                "event_id": rec.event_id,
                "topic": rec.topic,
                "ts": rec.ts,
                "payload": rec.payload,
                "blob_path": rec.blob_path,
            },
            default=str,
        )

    return fetch_event


def make_recall_tool(store: Store) -> BaseTool:
    """Bind the store to a fresh ``recall`` tool for the orchestrator.

    Returns a tool that surfaces recent decision history (one-line summaries)
    so the orchestrator can answer "have we already handled something like
    this?" without ever loading prior conversations.
    """

    @tool
    async def recall(topic: str, since: str = "1d") -> str:
        """Recall recent decisions matching a topic glob (e.g. 'fs.*').

        ``since`` accepts ``Nh`` / ``Nd`` (default ``1d``). Returns up to 20
        one-line summaries as a JSON array. Useful for spotting duplicates or
        recurring patterns; do not rely on this for state — the truth is the
        filesystem and the event store.
        """
        secs = _parse_since(since)
        rows = await store.recall(topic, since_sec=secs, limit=20)
        return json.dumps(rows, default=str)

    return recall


def _parse_since(s: str) -> float:
    s = s.strip().lower()
    if not s:
        return 24 * 3600.0
    unit = s[-1]
    try:
        n = float(s[:-1])
    except ValueError:
        return 24 * 3600.0
    if unit == "h":
        return n * 3600.0
    if unit == "d":
        return n * 86400.0
    if unit == "m":
        return n * 60.0
    return n  # bare seconds
