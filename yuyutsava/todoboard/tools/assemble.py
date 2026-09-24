"""Assemble the ``todo_*`` families for one scope."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.todoboard.exchange import TodoExchange, get_default_exchange
from yuyutsava.todoboard.tools.artifacts import make_artifacts_tools
from yuyutsava.todoboard.tools.capture import make_capture_tools
from yuyutsava.todoboard.tools.editing import make_editing_tools
from yuyutsava.todoboard.tools.notes import make_notes_tools
from yuyutsava.todoboard.tools.support import BoardContext, ToolScope


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
