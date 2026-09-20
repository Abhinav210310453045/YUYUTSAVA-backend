"""This session's model spend: reading it, and saying it in one line.

The REPL used to query ``llm_usage`` itself, which put a storage read in the
module that draws the prompt. Both halves are here instead, and both are
quiet by contract — a usage figure is a nicety, and nothing about reporting it
may interrupt a conversation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("yuyutsava.cli.usage")


async def session_usage(store: Any, thread_id: str, since: float) -> list[Any]:
    """This thread's ``llm_usage`` rows since *since*. Never raises.

    Filtered by ``thread_id`` in the query, not afterwards: fetching a window
    by time and discarding other conversations' rows in Python silently
    truncates this session's own on a busy machine.
    """
    if store is None or not thread_id:
        return []
    try:
        return await store.list(thread_id=thread_id, since=since, limit=2_000)
    except Exception:  # noqa: BLE001 — reporting must never break the REPL
        logger.debug("usage read failed", exc_info=True)
        return []


def format_usage(rows: list[Any]) -> str:
    """``3 calls · in 41,087 (↺93%) · out 64 · ~$0.0032``.

    Empty when there is nothing to say. The cache share is shown only when the
    provider actually reported one: a 0 there means "no cache detail
    reported", which is not the same as a 0 % hit rate.
    """
    if not rows:
        return ""
    calls = len(rows)
    tin = sum(r.input_tokens for r in rows)
    tout = sum(r.output_tokens for r in rows)
    cost = sum(r.est_cost_usd for r in rows)
    cached = sum(getattr(r, "cache_read_tokens", 0) for r in rows)
    cache_note = f" (↺{cached / tin:.0%})" if cached and tin else ""
    out = f"{calls} call{'s' if calls != 1 else ''} · in {tin:,}{cache_note} · out {tout:,}"
    # A zero estimate means the model is missing from the price table, which
    # is not the same as free — say nothing rather than "$0.00".
    return f"{out} · ~${cost:,.4f}" if cost > 0 else out


__all__ = ["format_usage", "session_usage"]
