"""Render top-k retrieval hits into a hard-capped, informational prompt block.

Generic version of the original ``MemoryInjector``: searches the store with the
task text and renders the hits into a budget-bounded block, fitting *whole*
entries rather than slicing at a raw char boundary (only an oversized first
entry is truncated, so a later entry is never emitted as a fragment).

``build_block`` never raises — retrieval is an enhancement, not a dependency; a
failed recall must not stop a task from running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.store import VectorStore

logger = logging.getLogger("yuyutsava.retrieval.injector")

# Chars of the block each injector most recently rendered, keyed by its prefix.
# These blocks are appended to the system prompt at *model-call* time, after
# every ``before_model`` hook has already seen the message list — so an
# observer counting what is in the window cannot see them and would under-report
# the prompt by however much was recalled. This is the same process-global,
# purely-diagnostic shape as ``skills.injector._last_recalled``: nothing
# branches on it, and in a daemon serving concurrent threads it can be one turn
# stale for a given conversation.
_last_blocks: dict[str, int] = {}


def last_block_chars() -> dict[str, int]:
    """``prefix -> chars`` for the most recent block each injector rendered."""
    return dict(_last_blocks)


class RetrievalInjector:
    """Renders top-k relevant hits for a task into a prompt block."""

    def __init__(
        self,
        store: VectorStore,
        *,
        top_k: int,
        prefix: str,
        budget_chars: int,
        render: Callable[[Hit], str],
        query_cap: int = 1000,
        search_kwargs: dict | None = None,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._prefix = prefix
        self._budget = budget_chars
        self._render = render
        self._query_cap = query_cap
        # Extra per-domain filters forwarded to store.search (e.g. {"agent": …}).
        self._search_kwargs = search_kwargs or {}

    async def build_block(self, task_text: str) -> str:
        """Return the rendered block, or empty string. Never raises."""
        block = await self._render_block(task_text)
        # Recorded on every path, empty included: "nothing was recalled this
        # turn" is the answer a meter needs, not a missing key from last turn.
        _last_blocks[self._prefix] = len(block)
        return block

    async def _render_block(self, task_text: str) -> str:
        if not task_text.strip():
            return ""
        try:
            hits = await self._store.search(
                task_text[: self._query_cap], k=self._top_k, **self._search_kwargs
            )
        except Exception:
            logger.exception("retrieval injector: search failed — skipping block")
            return ""
        if not hits:
            return ""

        out = [self._prefix]
        used = len(self._prefix)
        for h in hits:
            line = self._render(h)
            remaining = self._budget - used - 1  # account for the join newline
            if remaining <= 0:
                break
            if len(line) > remaining:
                if len(out) == 1:  # nothing fit yet — show a truncated first hit
                    out.append(line[:remaining])
                break
            out.append(line)
            used += len(line) + 1
        if len(out) == 1:
            return ""
        return "\n".join(out)
