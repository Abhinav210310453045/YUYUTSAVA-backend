"""TODO board tools: recording a card and finding it again.

Split out of the 266-line ``make_todo_tools`` (`R-RDB-02`); the factory in
``__init__`` composes the families.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.todoboard.tools.support import (
    BoardContext,
    render_card,
    render_summary,
    safe,
)

logger = logging.getLogger("yuyutsava.todoboard.tools.capture")


def make_capture_tools(ctx: BoardContext) -> list[BaseTool]:
    """Build the capture tools bound to one exchange."""
    ex, author = ctx.exchange, ctx.author

    @tool
    @safe
    async def todo_add(
        title: str,
        note: str | None = None,
        tags: list[str] | None = None,
        status: str = "inbox",
    ) -> str:
        """Add a card to the user's global TODO board.

        Use when the user asks to remember, plan, or track a task/idea
        ("add this as a TODO", "put it on my board"). ``title`` is a short
        renamable headline; put any detail/context into ``note``. ``status``
        is one of: inbox, active, done, archived (default inbox).
        """
        card = await ex.add_card(title, status=status, tags=tags, note=note, note_author=author)
        return f"created TODO card {card.card_id}: {card.title!r} [{card.status}]"

    @tool
    @safe
    async def todo_list(
        status: str | None = None,
        tag: str | None = None,
        limit: int = 25,
    ) -> str:
        """List cards on the user's global TODO board (pinned first, then most
        recently updated).

        Filter with ``status`` (inbox/active/done/archived) and/or ``tag``.
        Returns one line per card with its id — use todo_get for details.
        """
        cards = await ex.query_board(status=status, tag=tag, limit=limit)
        if not cards:
            return "the TODO board has no matching cards"
        return "\n".join(render_summary(c) for c in cards)

    @tool
    @safe
    async def todo_get(card_id: str) -> str:
        """Read one TODO card in full: title, status, tags, objectives (its
        think flow), all notes and attachments, the card's workspace directory
        path, and its most recent activity."""
        card = await ex.get_card(card_id)
        events = await ex.list_events(card_id)
        return render_card(card, events[-8:])

    @tool
    @safe
    async def todo_recall(query: str, card_id: str | None = None, k: int = 6) -> str:
        """Semantically search the notes on the user's TODO board — decisions,
        ideas, findings written on cards. Use when the user refers to something
        that may live on the board ("what did we decide about X?", "the plan on
        my board"). Pass ``card_id`` to search one card only. Returns matching
        note excerpts labeled with their card id — read the full card with
        todo_get."""
        hits = await ex.search_notes(query, k=k, card_id=card_id)
        if not hits:
            return (
                "no board notes matched (semantic recall may be unavailable "
                "on this deployment — try todo_list / todo_get)"
            )
        lines = []
        for h in hits:
            card = h.payload.get("card_id", "?")
            text = h.text if len(h.text) <= 300 else h.text[:300] + " …"
            lines.append(f"- [{card}] {text}")
        return "\n".join(lines)

    return [
        todo_add,
        todo_list,
        todo_get,
        todo_recall,
    ]
