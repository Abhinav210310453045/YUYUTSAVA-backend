"""Context tools: compacting the window on purpose.

One tool, and it takes no dependencies: compaction is requested through the run's own context rather than a store.

Split out of the 242-line ``make_context_tools`` (`R-RDB-02`).
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.compaction import request_compaction
from yuyutsava.run_context import thread_id_from_runtime

logger = logging.getLogger("yuyutsava.context.tools.compaction")


def make_compaction_tools() -> list[BaseTool]:
    """Build the compaction tools."""

    @tool
    async def ctx_compact() -> str:
        """Compact this conversation's older turns into a summary now.

        Use when the context is getting long and the older turns are no
        longer needed verbatim — e.g. after finishing a subtask, or before
        starting something new in the same conversation. The summary keeps
        the session intent, decisions, and artifact ids; recent messages
        stay verbatim. Compaction runs automatically near the token budget,
        so only call this to compact EARLIER than that.
        """
        thread_id = thread_id_from_runtime()
        if not thread_id:
            return "[error] no active thread — compaction not scheduled"
        request_compaction(thread_id)
        return (
            "[ok] compaction scheduled — older turns will be summarized on "
            "the next model call in this thread"
        )

    return [
        ctx_compact,
    ]
