"""mem_* tools: agent-facing search/save over the memory store.

Hidden behind the normal ``tool_search`` discovery flow (the ``mem_``
prefix is suppressed by ``ToolFilterPolicy``) — unlike ctx_*,
nothing in a tool result forces the model to need these immediately, and
the orchestrator prompt tells it they exist.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.memory.store import VALID_KINDS, MemoryStore

logger = logging.getLogger("yuyutsava.memory.tools")


def make_memory_tools(store: MemoryStore) -> list[BaseTool]:
    """Build the mem_* tool pair bound to one memory store."""

    @tool
    async def mem_search(query: str, k: int = 5) -> str:
        """Search long-term memory for relevant past context.

        Memory holds summaries of past sessions, outcomes of completed
        tasks, and durable facts about the user/projects. Use it when a
        task references past work ("like last time", "the report we made")
        or when knowing prior outcomes would change your approach.
        """
        hits = await store.search(query, k=k)
        if not hits:
            return "no relevant memories found"
        lines = []
        for h in hits:
            score = f" (score {h.score:.2f})" if h.score else ""
            lines.append(f"- [{h.kind}]{score} {h.text[:600]}")
        return "\n".join(lines)

    @tool
    async def mem_save(text: str, kind: str = "fact") -> str:
        """Save a durable memory for future sessions.

        Use for facts worth remembering across tasks: user preferences and
        constraints, project conventions, decisions with lasting effect.
        Do NOT save transient task state — summaries handle that
        automatically. ``kind`` is one of: fact, preference, task_outcome,
        summary.
        """
        if kind not in VALID_KINDS:
            return f"invalid kind {kind!r}; use one of {', '.join(VALID_KINDS)}"
        memory_id = await store.add(kind=kind, text=text)
        return f"saved {memory_id}"

    return [mem_search, mem_save]
