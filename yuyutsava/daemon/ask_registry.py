"""Durable index of Tier-2 asks awaiting an answer.

An ask is the one thing in the system that is allowed to block an agent
*indefinitely*. Nothing expires: the graph is parked on a LangGraph
``interrupt()`` and stays there until the user replies. That makes the ask the
one piece of HITL state which must not live only in a process's memory —
before this, a converse ask had no id at all and auto-rejected after 300 s,
while a hub ask blocked forever with no way to rediscover it (``WebHub.broadcast``
silently drops on ``QueueFull``, and asks carry no ``task_id`` so the per-task
replay ring can't help either).

This registry is the fix, and it is deliberately dull:

* :meth:`record` writes the row **before** the ask is broadcast, so a frame
  lost on the wire is still discoverable via ``GET /asks``.
* :meth:`resolve` flips it with a compare-and-set, so "first answer anywhere
  wins" holds when two surfaces answer in the same instant.
* :meth:`hydrate` reloads what was pending at boot, which is what lets an
  answer given *after* a daemon restart still reach the agent (the re-entry
  itself is :class:`~yuyutsava.daemon.ask_resume.AskResumeService`'s job).

The in-memory mirror exists so ``GET /asks`` and the resume path are a dict
lookup rather than a query on the HITL hot path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yuyutsava.daemon.channels import AskPrompt

logger = logging.getLogger("yuyutsava.daemon.ask_registry")


class AskRegistry:
    """Persisted pending asks + an in-memory mirror of them."""

    def __init__(self, store: Any) -> None:
        self._store = store
        # ask_id -> wire record (exactly what every surface renders from).
        self._pending: dict[str, dict[str, Any]] = {}
        # ask_ids that were pending in a PREVIOUS daemon process. An answer to
        # one of these has no in-memory future to resolve, so it needs the
        # resume path instead of a plain future.set_result.
        self._orphaned: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Write path                                                          #
    # ------------------------------------------------------------------ #

    async def record(self, ask: AskPrompt) -> dict[str, Any]:
        """Persist and index an ask. Call this BEFORE broadcasting it.

        Never raises: an ask that can't be written is still worth showing, and
        failing the HITL prompt because the DB hiccupped would strand the agent
        far more thoroughly than losing its durability would.
        """
        record = ask.to_wire_dict()
        async with self._lock:
            self._pending[ask.ask_id] = record
        try:
            await self._store.put_pending_ask(record)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ask %s could not be persisted — it is live but will not "
                "survive a restart", ask.ask_id, exc_info=True,
            )
        return record

    async def resolve(
        self, ask_id: str, response: str, *, status: str = "answered"
    ) -> bool:
        """Mark an ask resolved. False when it was already resolved.

        The DB compare-and-set is authoritative, so two surfaces answering
        simultaneously produce exactly one winner.
        """
        async with self._lock:
            self._pending.pop(ask_id, None)
            self._orphaned.discard(ask_id)
        try:
            return await self._store.resolve_pending_ask(ask_id, response, status=status)
        except Exception:  # noqa: BLE001
            logger.warning("ask %s resolve write failed", ask_id, exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Read path                                                           #
    # ------------------------------------------------------------------ #

    def pending(self) -> list[dict[str, Any]]:
        """Every unanswered ask, oldest first. Feeds ``GET /asks``."""
        return sorted(
            self._pending.values(), key=lambda r: r.get("created_ts") or 0.0
        )

    def get(self, ask_id: str) -> dict[str, Any] | None:
        return self._pending.get(ask_id)

    def is_orphaned(self, ask_id: str) -> bool:
        """True when this ask survived a daemon restart.

        Its agent is still parked on the checkpointed interrupt, but nothing in
        *this* process is awaiting a future for it — answering means re-entering
        the graph rather than waking a waiter.
        """
        return ask_id in self._orphaned

    # ------------------------------------------------------------------ #
    # Boot                                                                #
    # ------------------------------------------------------------------ #

    async def hydrate(self) -> int:
        """Reload asks left pending by a previous process. Returns the count."""
        try:
            rows = await self._store.list_pending_asks()
        except Exception:  # noqa: BLE001
            logger.warning("pending-ask hydration failed", exc_info=True)
            return 0
        async with self._lock:
            for rec in rows:
                ask_id = rec.get("ask_id")
                if not ask_id or ask_id in self._pending:
                    continue
                self._pending[ask_id] = rec
                self._orphaned.add(ask_id)
        if rows:
            logger.info(
                "ask registry: %d ask(s) still pending from a previous run — "
                "answering one resumes its agent", len(rows),
            )
        return len(rows)
