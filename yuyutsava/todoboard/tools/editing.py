"""TODO board tools: changing a card itself.

Split out of the 266-line ``make_todo_tools`` (`R-RDB-02`); the factory in
``__init__`` composes the families.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.todoboard.tools.support import (
    BoardContext,
    safe,
)

logger = logging.getLogger("yuyutsava.todoboard.tools.editing")


def make_editing_tools(ctx: BoardContext) -> list[BaseTool]:
    """Build the editing tools bound to one exchange."""
    ex, author = ctx.exchange, ctx.author

    @tool
    @safe
    async def todo_update(
        card_id: str,
        title: str | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Update a TODO card's title, status, pinned flag, or tags. Only the
        fields you pass change; the rest stay as they are."""
        card = await ex.update_card(
            card_id,
            title=title,
            status=status,
            pinned=pinned,
            tags=tags,
            actor=author,
        )
        return f"updated {card.card_id}: {card.title!r} [{card.status}]"

    @tool
    @safe
    async def todo_set_status(card_id: str, status: str) -> str:
        """Move a TODO card to a new status: inbox, active, done, or archived."""
        card = await ex.update_card(card_id, status=status, actor=author)
        return f"{card.card_id} is now [{card.status}]"

    return [
        todo_update,
        todo_set_status,
    ]
