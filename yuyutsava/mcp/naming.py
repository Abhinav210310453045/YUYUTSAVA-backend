"""How an MCP tool is named among an agent's tools: ``<server>__<tool>``.

The server prefix lets two servers each expose ``read`` without a collision,
and it is how the rest of the system tells an MCP tool from a built-in one —
the tool filter hides MCP schemas until ``tool_search`` loads them, and the
catalog lists them per server. No built-in tool name contains the separator.

Free of the MCP SDK, so reading a tool name never loads it.
"""

from __future__ import annotations

SEPARATOR = "__"


def tool_name(server: str, tool: str) -> str:
    """The agent-facing name of *server*'s *tool*."""
    return f"{server}{SEPARATOR}{tool}"


def server_of(name: str) -> str | None:
    """The MCP server a tool name belongs to; ``None`` for a built-in tool."""
    server, sep, tool = name.partition(SEPARATOR)
    return server if sep and server and tool else None
