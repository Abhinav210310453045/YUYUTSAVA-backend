"""Adapt ``mcp.types.Tool`` instances into ``langchain_core.tools.BaseTool``.

Tool names are namespaced ``<server>__<tool>`` so two servers can both expose
``read`` without collision. Result content blocks are flattened to a single
string for now; image / binary results are deferred to a later phase.

The ``ClientSession`` lives on the MCPClientManager's loop (the daemon main
loop) and its anyio cancel scopes must never run elsewhere — but the adapted
tools are also baked into background subagent graphs, which execute on the
AsyncSubagentHost's uvicorn loop. Calls from a foreign loop are therefore
marshaled onto the session's home loop via ``run_coroutine_threadsafe``; the
caller just awaits the wrapped future. See Architecture.md "Event-loop
ownership".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession
from mcp.types import Tool as MCPTool

logger = logging.getLogger("yuyutsava.mcp.tool_adapter")


def adapt(session: ClientSession, server_name: str, mcp_tool: MCPTool) -> BaseTool:
    """Wrap one MCP tool as a LangChain ``StructuredTool``.

    The returned tool's name is ``<server_name>__<tool_name>``; its
    ``args_schema`` is the MCP tool's input schema (a JSON Schema dict), read
    with :func:`sdk_field` because the SDK renamed it between majors.
    Invoking the tool calls ``session.call_tool`` and flattens text content
    blocks to a single string.
    """
    namespaced = f"{server_name}__{mcp_tool.name}"
    description = mcp_tool.description or f"MCP tool {namespaced}"
    input_schema = dict(
        sdk_field(mcp_tool, "input_schema", "inputSchema") or {"type": "object", "properties": {}}
    )
    # adapt() runs inside the manager's _run_server task, so this IS the loop
    # the session (and its cancel scopes) belongs to.
    home_loop = asyncio.get_running_loop()

    async def _call(**kwargs: Any) -> str:
        try:
            if asyncio.get_running_loop() is home_loop:
                result = await session.call_tool(mcp_tool.name, kwargs)
            else:
                result = await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(
                        session.call_tool(mcp_tool.name, kwargs), home_loop
                    )
                )
        except Exception as exc:  # noqa: BLE001 — a call's failure goes back to the model as text
            logger.warning("MCP call %s failed: %s", namespaced, exc)
            return f"[error calling {namespaced}: {exc}]"
        return result_text(result)

    return StructuredTool.from_function(
        name=namespaced,
        description=description,
        args_schema=input_schema,
        coroutine=_call,
    )


def sdk_field(obj: Any, snake: str, camel: str) -> Any:
    """An MCP SDK model field under either major's spelling; ``None`` if absent.

    mcp 1.x models carry the wire protocol's camelCase as attribute names
    (``Tool.inputSchema``, ``CallToolResult.isError``); mcp 2.x renamed the
    attributes to snake_case and keeps camelCase only as serialisation aliases.
    Reading one spelling breaks on the other major: on 2.x ``inputSchema``
    made every server fail to load (``'Tool' object has no attribute
    'inputSchema'``) and ``isError`` read as absent, so a failed call was
    reported to the model as a success. On 1.x ``input_schema`` failed the
    same way (`R-BUG-10`). The project supports both (``pyproject.toml``
    ``mcp>=1.26,<3``), so every such field is read through here.
    """
    value = getattr(obj, snake, None)
    return getattr(obj, camel, None) if value is None else value


def result_text(result: Any) -> str:
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
    is_error = bool(sdk_field(result, "is_error", "isError"))
    body = "\n".join(pieces).strip()
    return f"[error] {body}" if is_error else body
