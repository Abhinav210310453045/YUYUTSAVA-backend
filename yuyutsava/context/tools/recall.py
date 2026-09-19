"""Context tools: semantic recall over offloaded results.

Only meaningful when the store maintains a semantic index (Postgres plus an embedder); the factory does not offer it otherwise, so the surface degrades cleanly.

Split out of the 242-line ``make_context_tools`` (`R-RDB-02`).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.run_context import thread_id_from_runtime

logger = logging.getLogger("yuyutsava.context.tools.recall")


def make_recall_tools(store: Any) -> list[BaseTool]:
    """Build the recall tools."""

    @tool
    async def ctx_recall(query: str, k: int = 5) -> str:
        """Semantically recall relevant slices of earlier offloaded results.

        Searches everything offloaded in *this* conversation (web searches,
        large reads) for passages related to ``query`` and returns the best
        matches with their ``artifact_id`` and ``char_offset``. Use this when
        you need detail from an earlier tool result instead of re-running the
        tool; then ctx_fetch_artifact(artifact_id, offset=char_offset) for the
        full surrounding text.
        """
        hits = await store.recall(thread_id_from_runtime(), query, k=k)  # type: ignore[attr-defined]
        if not hits:
            return f"[no offloaded results matched {query!r}]"
        lines = [
            f"{i}. artifact={h.artifact_id} offset={h.char_offset} "
            f"score={h.score:.3f}\n   {h.snippet}"
            for i, h in enumerate(hits)
        ]
        return (
            "[ctx_recall hits — fetch full text via ctx_fetch_artifact(artifact_id, offset)]\n"
            + "\n".join(lines)
        )

    return [
        ctx_recall,
    ]
