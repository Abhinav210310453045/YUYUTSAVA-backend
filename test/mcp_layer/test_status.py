"""The agent is told which MCP connectors it has, and why one is missing (R-UI-12).

Asked about its connectors, the agent had nothing to go on — a server that
failed to start was only a daemon log line — and went reading Claude Desktop's
MCP configuration instead. The manager now records each configured server's
state, and the masters' prompts carry it.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.mcp.status import CONNECTED, FAILED, ServerStatus, connectors_block

CONFIG = Path.home() / ".yuyutsava" / "mcp_config.json"


class TheBlock(unittest.TestCase):
    def test_connected_and_failed_servers_and_where_they_are_configured(self) -> None:
        block = connectors_block(
            [
                ServerStatus("gdrive", CONNECTED, tools=128),
                ServerStatus("notion", FAILED, error="FileNotFoundError: npx"),
            ],
            CONFIG,
        )
        self.assertIn("configured in ~/.yuyutsava/mcp_config.json", block)
        self.assertIn("gdrive: connected, 128 tools (gdrive__*)", block)
        self.assertIn("notion: failed — FileNotFoundError: npx", block)
        self.assertIn("Never read another app's MCP configuration", block)

    def test_no_servers_adds_nothing_to_the_prompt(self) -> None:
        self.assertEqual(connectors_block([], CONFIG), "")


def _config(*servers: MCPServerSpec, scopes: dict | None = None) -> MCPConfig:
    return MCPConfig(
        servers={s.name: s for s in servers},
        scopes=scopes or {},
        default_scope=[s.name for s in servers],
    )


class TheManagerRecordsIt(unittest.TestCase):
    def test_a_server_that_cannot_start_is_failed_with_its_reason(self) -> None:
        broken = MCPServerSpec(name="broken", command="/nonexistent/mcp-server-binary")

        async def go():
            manager = MCPClientManager()
            await manager.start(_config(broken))
            try:
                return manager.status("cli"), manager.connectors_block("cli")
            finally:
                await manager.stop()

        with self.assertLogs("yuyutsava.mcp.loader", "ERROR"):
            statuses, block = asyncio.run(asyncio.wait_for(go(), 30))
        [status] = statuses
        self.assertEqual((status.name, status.state), ("broken", FAILED))
        self.assertIn("mcp-server-binary", status.error)
        self.assertNotIn("sub-exception", status.error, "the innermost cause, not the group")
        self.assertIn("broken: failed —", block)

    def test_status_is_scoped_to_the_agent(self) -> None:
        a = MCPServerSpec(name="a", command="/nonexistent/a")
        b = MCPServerSpec(name="b", command="/nonexistent/b")

        async def go():
            manager = MCPClientManager()
            await manager.start(_config(a, b, scopes={"tinker": ["b"]}))
            try:
                return [s.name for s in manager.status("tinker")], [
                    s.name for s in manager.status()
                ]
            finally:
                await manager.stop()

        with self.assertLogs("yuyutsava.mcp.loader", "ERROR"):
            tinker, everyone = asyncio.run(asyncio.wait_for(go(), 30))
        self.assertEqual((tinker, sorted(everyone)), (["b"], ["a", "b"]))


if __name__ == "__main__":
    unittest.main()
