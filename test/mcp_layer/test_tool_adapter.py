"""The MCP tool adapter, driven with the SDK's real types.

`R-BUG-10`: renaming `inputSchema` to `input_schema` — reasonable-looking, since
the rest of the codebase is snake_case — made every MCP server fail to load with
``'Tool' object has no attribute 'input_schema'``. Nothing caught it because the
adapter had no tests at all, and the loader logs the failure per server and
carries on, so the daemon still starts and looks healthy.

These use ``mcp.types.Tool`` and ``mcp.types.CallToolResult`` rather than stubs.
A stub would happily grow whatever attribute the adapter asked for, which is
precisely the bug it needs to catch.

`R-BUG-25`: the fix above broke the other way on mcp 2.x, which renamed these
attributes to snake_case (``input_schema``, ``is_error``) — the daemon, installed
unlocked, resolved 2.2.0 and every server failed with ``'Tool' object has no
attribute 'inputSchema'``, while ``isError`` silently read as absent. So each
test also runs against a 2.x-shaped object: plain attributes, nothing invented.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent, Tool

from yuyutsava.mcp.tool_adapter import adapt, result_text, sdk_field

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
        self.assertEqual(result_text(result), "first\nsecond")

    async def test_an_error_result_is_marked(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="boom")], isError=True)
        self.assertEqual(result_text(result), "[error] boom")

    async def test_no_result_is_empty(self) -> None:
        self.assertEqual(result_text(None), "")


def _v2_tool(**kw):
    """An mcp 2.x ``Tool`` as the adapter sees it: snake_case attributes only."""
    return SimpleNamespace(
        name=kw["name"], description=kw.get("description"), input_schema=kw["input_schema"]
    )


def _v2_result(text: str, *, is_error: bool) -> SimpleNamespace:
    return SimpleNamespace(content=[TextContent(type="text", text=text)], is_error=is_error)


class TheOtherMajorsSpelling(unittest.IsolatedAsyncioTestCase):
    async def test_a_2x_tool_adapts_with_its_schema(self) -> None:
        adapted = adapt(_StubSession(), "gdrive", _v2_tool(name="read_file", input_schema=SCHEMA))
        self.assertEqual(adapted.args_schema, SCHEMA)

    async def test_a_2x_error_result_is_marked_and_a_success_is_not(self) -> None:
        self.assertEqual(result_text(_v2_result("boom", is_error=True)), "[error] boom")
        self.assertEqual(result_text(_v2_result("fine", is_error=False)), "fine")

    async def test_either_spelling_is_read_and_a_false_value_is_kept(self) -> None:
        self.assertEqual(sdk_field(SimpleNamespace(isError=True), "is_error", "isError"), True)
        self.assertEqual(sdk_field(SimpleNamespace(is_error=False), "is_error", "isError"), False)
        self.assertIsNone(sdk_field(SimpleNamespace(), "is_error", "isError"))


if __name__ == "__main__":
    unittest.main()
