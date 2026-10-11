"""MCP servers start together, within a time limit, and a chat need not wait for them.

Real servers (``slow_server.py``, an MCP server that takes a set time to come up)
over stdio, as the CLI runs a user's ``npx`` servers.
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.mcp.status import CONNECTED, FAILED, STARTING

SERVER = str(Path(__file__).with_name("slow_server.py"))


def _slow(name: str, delay: float) -> MCPServerSpec:
    return MCPServerSpec(name=name, command=sys.executable, args=[SERVER, "--delay", str(delay)])


def _config(*servers: MCPServerSpec) -> MCPConfig:
    return MCPConfig(
        servers={s.name: s for s in servers}, scopes={}, default_scope=[s.name for s in servers]
    )


def _states(manager: MCPClientManager) -> dict[str, str]:
    return {s.name: s.state for s in manager.status()}


class Together(unittest.TestCase):
    def test_three_slow_servers_take_as_long_as_one(self) -> None:
        async def go() -> tuple[float, dict[str, str]]:
            manager = MCPClientManager()
            began = time.monotonic()
            await manager.start(_config(_slow("a", 1.5), _slow("b", 1.5), _slow("c", 1.5)))
            took = time.monotonic() - began
            try:
                return took, _states(manager)
            finally:
                await manager.stop()

        took, states = asyncio.run(asyncio.wait_for(go(), 60))
        self.assertEqual(states, {"a": CONNECTED, "b": CONNECTED, "c": CONNECTED})
        self.assertLess(took, 4.5, "one after another would take 4.5 s and more")

    def test_one_past_its_time_limit_is_given_up_and_the_rest_are_not(self) -> None:
        async def go() -> tuple[dict[str, str], str, list[str]]:
            manager = MCPClientManager(start_timeout=3)
            await manager.start(_config(_slow("quick", 0), _slow("stuck", 30)))
            try:
                stuck = next(s for s in manager.status() if s.name == "stuck")
                return _states(manager), stuck.error, [t.name for t in manager.tools_for("cli")]
            finally:
                await manager.stop()

        with self.assertLogs("yuyutsava.mcp.loader", "WARNING"):
            states, why, tools = asyncio.run(asyncio.wait_for(go(), 60))
        self.assertEqual(states, {"quick": CONNECTED, "stuck": FAILED})
        self.assertIn("did not start within 3 s", why)
        self.assertTrue(any("echo" in name for name in tools))


class InTheBackground(unittest.TestCase):
    def test_begin_returns_at_once_and_says_starting_until_the_server_is_up(self) -> None:
        async def go():
            manager = MCPClientManager()
            heard: list[int] = []
            manager.subscribe(lambda: heard.append(manager.generation))
            began = time.monotonic()
            manager.begin(_config(_slow("late", 1.5)))
            returned_after = time.monotonic() - began
            before = (_states(manager), manager.tools_for("cli"), manager.connectors_block("cli"))
            while not manager.settled() or not heard:
                await asyncio.sleep(0.1)
            after = (_states(manager), [t.name for t in manager.tools_for("cli")])
            await manager.stop()
            return returned_after, before, after, heard

        returned_after, before, after, heard = asyncio.run(asyncio.wait_for(go(), 60))
        self.assertLess(returned_after, 0.5)
        states, tools, block = before
        self.assertEqual((states, tools), ({"late": STARTING}, []))
        self.assertIn("late: still starting", block)
        self.assertNotIn("late: failed", block)
        self.assertEqual(after[0], {"late": CONNECTED})
        self.assertTrue(any("echo" in name for name in after[1]))
        self.assertEqual(heard, [1])

    def test_stopping_does_not_wait_for_a_slow_start(self) -> None:
        async def go() -> float:
            manager = MCPClientManager()
            manager.begin(_config(_slow("slow", 30)))
            await asyncio.sleep(0.3)
            began = time.monotonic()
            await manager.stop()
            return time.monotonic() - began

        self.assertLess(asyncio.run(asyncio.wait_for(go(), 60)), 5)


if __name__ == "__main__":
    unittest.main()
