"""The MCP tool adapter, driven with the SDK's real types.

`R-BUG-10`: renaming `inputSchema` to `input_schema` — reasonable-looking, since
the rest of the codebase is snake_case — made every MCP server fail to load with
``'Tool' object has no attribute 'input_schema'``. Nothing caught it because the
adapter had no tests at all, and the loader logs the failure per server and
carries on, so the daemon still starts and looks healthy.

These use ``mcp.types.Tool`` and ``mcp.types.CallToolResult`` rather than stubs.
A stub would happily grow whatever attribute the adapter asked for, which is
precisely the bug it needs to catch: these fields keep the wire protocol's
camelCase spelling and the adapter must use it.
"""

from __future__ import annotations

import unittest

from mcp.types import CallToolResult, TextContent, Tool

from yuyutsava.mcp.tool_adapter import _flatten_content, adapt

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


class _StubSession:
    """Stands in for ``ClientSession``; ``adapt`` only stores it for later."""


class AdaptingAnMcpTool(unittest.IsolatedAsyncioTestCase):
    async def test_the_tool_carries_the_servers_schema(self) -> None:
        tool = Tool(name="read_file", description="Read a file", inputSchema=SCHEMA)

        adapted = adapt(_StubSession(), "gdrive", tool)

        # Namespaced so two servers may both expose `read_file`.
        self.assertEqual(adapted.name, "gdrive__read_file")
        self.assertEqual(adapted.description, "Read a file")
        self.assertEqual(adapted.args_schema, SCHEMA)

    async def test_a_tool_without_a_description_still_adapts(self) -> None:
        tool = Tool(name="ping", inputSchema={"type": "object", "properties": {}})
        adapted = adapt(_StubSession(), "srv", tool)
        self.assertEqual(adapted.description, "MCP tool srv__ping")


class FlatteningAResult(unittest.IsolatedAsyncioTestCase):
    async def test_text_blocks_are_joined(self) -> None:
        result = CallToolResult(
            content=[
                TextContent(type="text", text="first"),
                TextContent(type="text", text="second"),
            ]
        )
        self.assertEqual(_flatten_content(result), "first\nsecond")

    async def test_an_error_result_is_marked(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="boom")], isError=True)
        self.assertEqual(_flatten_content(result), "[error] boom")

    async def test_no_result_is_empty(self) -> None:
        self.assertEqual(_flatten_content(None), "")


if __name__ == "__main__":
    unittest.main()
