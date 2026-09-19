"""The ``todo_*`` tool family, bound to one board.

Two scopes, because two kinds of agent use the board differently. **capture** is
what the orchestrator and the chat master get: record a card, find one again,
and nothing that edits. **full** is the TinkerAgent's, which works a card rather
than filing it.

The tools are grouped by what they touch — :mod:`capture`, :mod:`editing`,
:mod:`notes` and :mod:`artifacts` — because they used to be twelve closures in
one 266-line function (`R-RDB-02`). The families are ordered here to match the
order the bound list has always had: the model sees the same tools in the same
places.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.todoboard.exchange import TodoExchange, get_default_exchange
from yuyutsava.todoboard.models import CARD_STATUSES
from yuyutsava.todoboard.tools.artifacts import make_artifacts_tools
from yuyutsava.todoboard.tools.capture import make_capture_tools
from yuyutsava.todoboard.tools.editing import make_editing_tools
from yuyutsava.todoboard.tools.notes import make_notes_tools
from yuyutsava.todoboard.tools.support import BoardContext, ToolScope

__all__ = ["CARD_STATUSES", "ToolScope", "make_todo_tools"]


def make_todo_tools(
    exchange: TodoExchange | None = None,
    *,
    scope: ToolScope = "capture",
    author: str = "master",
) -> list[BaseTool]:
    """Build the ``todo_*`` family bound to one exchange.

    ``author`` labels notes written through these tools, so a card's history
    says which agent did what.
    """
    ctx = BoardContext(exchange=exchange or get_default_exchange(), author=author)
    capture = make_capture_tools(ctx)
    if scope == "capture":
        return capture
    return [
        *capture,
        *make_editing_tools(ctx),
        *make_notes_tools(ctx),
        *make_artifacts_tools(ctx),
    ]
