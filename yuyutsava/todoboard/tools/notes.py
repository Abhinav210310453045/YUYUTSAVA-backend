"""TODO board tools: notes and objectives — the work hanging off a card.

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

logger = logging.getLogger("yuyutsava.todoboard.tools.notes")


def make_notes_tools(ctx: BoardContext) -> list[BaseTool]:
    """Build the notes tools bound to one exchange."""
    ex, author = ctx.exchange, ctx.author

    @tool
    @safe
    async def todo_add_note(
        card_id: str,
        body: str,
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> str:
        """Append a note to a TODO card — an idea, finding, decision, or next
        step worth keeping on the card. Pass ``objective_id`` to attach the
        note to the objective it serves (and optionally ``phase`` for the
        think-flow context it was written in); omit both for a card-level
        general note."""
        note = await ex.add_note(
            card_id, body, author=author, objective_id=objective_id, phase=phase
        )
        return f"added note {note.note_id} to {card_id}"

    @tool
    @safe
    async def todo_add_objective(
        card_id: str,
        title: str,
        phase: str = "thinking",
    ) -> str:
        """Add one small, independently-checkable objective to a card's think
        flow. Objectives are the card's structured decomposition (typically
        3-6); each moves through phases: thinking, planning, doing, completed,
        with blocked and abandoned as off-ramps."""
        obj = await ex.add_objective(card_id, title, phase=phase, actor=author)
        return f"added objective {obj.objective_id} [{obj.phase}] to {card_id}: {obj.title!r}"

    @tool
    @safe
    async def todo_update_objective(
        objective_id: str,
        title: str | None = None,
        phase: str | None = None,
        order: int | None = None,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> str:
        """Update an objective — move it through its think flow or refine it.

        ``phase`` is one of: thinking, planning, doing, completed, blocked,
        abandoned (any move is allowed). Give ``reason`` when moving to
        blocked/abandoned (why it stopped) and ``outcome`` when completing
        (what it produced) — the journey document weaves these in. ``order``
        repositions the objective in the card's list."""
        obj = await ex.update_objective(
            objective_id,
            title=title,
            phase=phase,
            order_idx=order,
            reason=reason,
            outcome=outcome,
            actor=author,
        )
        return f"objective {obj.objective_id} is now [{obj.phase}]: {obj.title!r}"

    @tool
    @safe
    async def todo_assign_note(
        note_id: str,
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> str:
        """Attach an existing note to an objective on the same card (or clear
        the assignment by passing no objective_id). Use this to convert older
        prose notes into the structured think flow."""
        note = await ex.assign_note(note_id, objective_id=objective_id, phase=phase, actor=author)
        target = f"objective {note.objective_id}" if note.objective_id else "card level (general)"
        return f"note {note.note_id} assigned to {target}"

    return [
        todo_add_note,
        todo_add_objective,
        todo_update_objective,
        todo_assign_note,
    ]
