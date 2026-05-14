"""Adapt ``mcp.types.Tool`` instances into ``langchain_core.tools.BaseTool``.

Tool names are namespaced ``<server>__<tool>`` so two servers can both expose
``read`` without collision. Result content blocks are flattened to a single
string for now; image / binary results are deferred to a later phase.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession
from mcp.types import Tool as MCPTool

logger = logging.getLogger("yuyutsava.mcp.tool_adapter")


def adapt(session: ClientSession, server_name: str, mcp_tool: MCPTool) -> BaseTool:
    """Wrap one MCP tool as a LangChain ``StructuredTool``.

    The returned tool's name is ``<server_name>__<tool_name>``; its
    ``args_schema`` is the MCP tool's ``inputSchema`` (a JSON Schema dict).
    Invoking the tool calls ``session.call_tool`` and flattens text content
    blocks to a single string.
    """
    namespaced = f"{server_name}__{mcp_tool.name}"
    description = mcp_tool.description or f"MCP tool {namespaced}"
    input_schema = dict(mcp_tool.inputSchema or {"type": "object", "properties": {}})

    async def _call(**kwargs: Any) -> str:
        try:
            result = await session.call_tool(mcp_tool.name, kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP call %s failed: %s", namespaced, exc)
            return f"[error calling {namespaced}: {exc}]"
        return _flatten_content(result)

    return StructuredTool.from_function(
        name=namespaced,
        description=description,
        args_schema=input_schema,
        coroutine=_call,
    )


def _flatten_content(result: Any) -> str:
    """Reduce ``CallToolResult`` (or compatible) to a single string.

    MCP tool results are a list of content blocks. We concatenate the ``text``
    of each text block; non-text blocks render as a short placeholder so the
    model knows something else was returned.
    """
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    pieces: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            pieces.append(str(text))
            continue
        kind = getattr(block, "type", type(block).__name__)
        pieces.append(f"[non-text content: {kind}]")
    is_error = getattr(result, "isError", False)
    body = "\n".join(pieces).strip()
    return f"[error] {body}" if is_error else body
