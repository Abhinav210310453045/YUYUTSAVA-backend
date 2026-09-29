"""Every tool an MCP server exposes is reachable, and none costs a schema up front.

The user's google-drive server exposed 128 tools; the loader kept the first 32
(``max_tools``) and dropped the rest, and the 32 it kept were bound with their
full schemas on every model call. Now all of them are bound, their schemas are
hidden until ``tool_search`` loads one (like ``ws_*``), and ``max_tools`` only
caps how many the prompt's catalog names — the rest are counted there and found
by search.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from langchain_core.tools import tool
from mcp.types import ListToolsResult, Tool

from yuyutsava.discovery.tool_registry import ToolRegistry
from yuyutsava.mcp.loader import list_all_tools
from yuyutsava.mcp.naming import server_of, tool_name
from yuyutsava.mcp.tool_adapter import adapt
from yuyutsava.policy.builtin.tool_filter import should_suppress


def _tools(server: str, n: int, *, limit: int = 32):
    return [
        adapt(
            object(),
            server,
            Tool(name=f"op_{i:03d}", description=f"Operation {i}.", inputSchema={"type": "object"}),
            catalog_limit=limit,
        )
        for i in range(n)
    ]


class Naming(unittest.TestCase):
    def test_a_tool_name_carries_its_server(self) -> None:
        self.assertEqual(tool_name("google_drive", "read"), "google_drive__read")
        self.assertEqual(server_of("google_drive__read"), "google_drive")

    def test_built_in_names_have_no_server(self) -> None:
        for name in ("tr_read_file", "tool_search", "write_todos", "__x", "srv__", "a_b"):
            with self.subTest(name=name):
                self.assertIsNone(server_of(name))


class TheFilterHidesMcpSchemas(unittest.TestCase):
    def test_mcp_tools_are_hidden_built_ins_unchanged(self) -> None:
        self.assertTrue(should_suppress("gdrive__search_files"))
        self.assertFalse(should_suppress("tool_search"))
        self.assertFalse(should_suppress("write_todos"))
        self.assertTrue(should_suppress("ws_tavily_search"))


class TheCatalog(unittest.IsolatedAsyncioTestCase):
    async def test_a_large_server_is_listed_up_to_its_cap_and_counted_beyond(self) -> None:
        registry = ToolRegistry()
        registry.register_many(_tools("google_drive", 40, limit=32))
        block = registry.catalog_block()
        self.assertIn("google_drive__*:", block)
        self.assertEqual(block.count("  - google_drive__op_"), 32)
        self.assertIn("… 8 more — find them with tool_search('google_drive__*')", block)

    async def test_an_unlisted_tool_is_still_found_by_select_and_glob(self) -> None:
        registry = ToolRegistry()
        registry.register_many(_tools("gdrive", 40, limit=32))
        catalog = registry.to_catalog()
        found = await catalog.search("select:gdrive__op_039", max_results=5)
        self.assertEqual([e.id for e in found.entries], ["gdrive__op_039"])
        globbed = await catalog.search("gdrive__op_03*", max_results=20)
        self.assertIn("gdrive__op_039", [e.id for e in globbed.entries])

    async def test_tool_search_serves_an_mcp_tools_parameters(self) -> None:
        """A JSON-Schema dict (what MCP tools carry) is rendered, not dropped."""
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        registry = ToolRegistry()
        registry.register(
            adapt(object(), "gdrive", Tool(name="search", description="Find.", inputSchema=schema))
        )
        with self.assertNoLogs("yuyutsava.discovery.tool_registry", "WARNING"):
            found = await registry.to_catalog().search("select:gdrive__search", max_results=1)
            detail = json.loads(found.entries[0].load_detail())
        self.assertEqual(detail[0]["parameters"], schema)

    async def test_built_in_groups_are_unchanged(self) -> None:
        @tool
        def tr_read_file(path: str) -> str:
            """Read a file."""
            return path

        registry = ToolRegistry()
        registry.register(tr_read_file)
        self.assertEqual(registry.catalog_block(), "tr_*:\n  - tr_read_file: Read a file.")


class _PagedSession:
    """``list_tools`` in three pages, as a server with a cursor serves it."""

    def __init__(self) -> None:
        self.cursors: list[str | None] = []

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params is not None else None
        self.cursors.append(cursor)
        page = {None: (0, "p2"), "p2": (1, "p3"), "p3": (2, None)}[cursor]
        return ListToolsResult(
            tools=[Tool(name=f"t{page[0]}", inputSchema={"type": "object"})],
            nextCursor=page[1],
        )


class Paging(unittest.TestCase):
    def test_every_page_is_read(self) -> None:
        session = _PagedSession()
        tools = asyncio.run(list_all_tools(session))
        self.assertEqual([t.name for t in tools], ["t0", "t1", "t2"])
        self.assertEqual(session.cursors, [None, "p2", "p3"])


if __name__ == "__main__":
    unittest.main()
