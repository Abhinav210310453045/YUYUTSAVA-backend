"""``AgentBundle.aclose`` — teardown order of the CLI-owned resources.

The MCP manager's sessions live on the bundle's loop and must be stopped
before the pool/embedder go away; a failing step must not prevent the rest
of the teardown. ``close()`` with nothing to close is a no-op.

Run:  .venv/bin/python test/core/test_agent_bundle_close.py
"""

from __future__ import annotations

import asyncio
import unittest

from yuyutsava.core.engine import AgentBundle


class _Rec:
    def __init__(self, log: list[str], name: str, *, fail: bool = False) -> None:
        self._log, self._name, self._fail = log, name, fail

    async def stop(self) -> None:
        self._log.append(f"{self._name}.stop")
        if self._fail:
            raise RuntimeError("boom")

    async def aclose(self) -> None:
        self._log.append(f"{self._name}.aclose")

    async def close(self) -> None:
        self._log.append(f"{self._name}.close")


class Teardown(unittest.TestCase):
    def test_mcp_stops_first_then_embedder_then_pool(self) -> None:
        log: list[str] = []
        bundle = AgentBundle(
            agent=None,  # type: ignore[arg-type]
            mcp_manager=_Rec(log, "mcp"),
            embedder=_Rec(log, "embedder"),
            pg_pool=_Rec(log, "pool"),
        )
        asyncio.run(bundle.aclose())
        self.assertEqual(log, ["mcp.stop", "embedder.aclose", "pool.close"])

    def test_a_failing_mcp_stop_does_not_block_the_rest(self) -> None:
        log: list[str] = []
        bundle = AgentBundle(
            agent=None,  # type: ignore[arg-type]
            mcp_manager=_Rec(log, "mcp", fail=True),
            pg_pool=_Rec(log, "pool"),
        )
        asyncio.run(bundle.aclose())
        self.assertEqual(log, ["mcp.stop", "pool.close"])

    def test_nothing_owned_is_a_noop(self) -> None:
        bundle = AgentBundle(agent=None)  # type: ignore[arg-type]
        asyncio.run(bundle.aclose())
        self.assertIsNone(bundle.mcp_manager)
        self.assertIsNone(bundle.layout)


if __name__ == "__main__":
    unittest.main()
