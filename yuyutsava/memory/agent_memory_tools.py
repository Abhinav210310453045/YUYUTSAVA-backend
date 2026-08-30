"""um_* tools — a master agent's private user-behavior memory.

Deliberately NOT tr_* file tools: ``~/.yuyutsava`` is the EXTERNAL zone for
every master, so tr_write_file would raise a user permission prompt on each
quiet learning write; and a dedicated tool enforces the slug/index/size
invariants that prompt-guided file writes would drift from. The ``um_``
prefix is suppressed by ToolFilterPolicy like the other families —
schemas load on demand via tool_search.
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import BaseTool, tool

from yuyutsava.memory.agent_memory import AgentMemoryStore


def make_agent_memory_tools(store: AgentMemoryStore) -> list[BaseTool]:
    """Return [um_note, um_read] bound to one agent's memory dir."""

    @tool
    async def um_note(name: str, summary: str, body: str = "") -> str:
        """Record a DURABLE behavior pattern of this user in your agent memory.

        Use for repeated, load-bearing observations about how THIS user works —
        phrasing habits, standing constraints, recurring corrections ("always
        answers in bullet points", "never wants auto-commits"). NOT for one-off
        facts (mem_save owns those) or task state. Reuse an existing note's
        name to update/consolidate it; keep ≤ 30 notes total.

        Args:
            name:    Short hyphenated identifier, e.g. 'prefers-terse-replies'.
            summary: One line (≤ 200 chars) — this is what future-you sees in
                     the AGENT MEMORY index every session, so make it carry
                     the whole point.
            body:    Optional detail (≤ 4000 chars): evidence, exceptions,
                     how to apply it.
        """
        return await asyncio.to_thread(store.write_note, name, summary, body)

    @tool
    async def um_read(name: str) -> str:
        """Read one agent-memory note in full by its index name.

        The AGENT MEMORY block in your prompt lists `[name] summary` lines;
        um_read(name) returns that note's complete body. Also the first step
        of consolidating or correcting a note (then um_note with the same
        name to rewrite it).
        """
        return await asyncio.to_thread(store.read_note, name)

    return [um_note, um_read]
