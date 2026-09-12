"""``start_manager_for_workspace`` — the standalone CLI's MCP lifecycle entry.

* zero configured servers → ``None`` (no manager is minted; the CLI behaves
  exactly as before MCP existed);
* configured servers → one manager from the factory, started once with the
  merged config — the daemon-provided path never reaches this helper.

Run:  .venv/bin/python test/mcp_layer/test_owned_manager.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.mcp.config import global_mcp_config_path
from yuyutsava.mcp.loader import start_manager_for_workspace


class _FakeManager:
    instances: list["_FakeManager"] = []

    def __init__(self) -> None:
        self.started_with = []
        self.stopped = 0
        _FakeManager.instances.append(self)

    async def start(self, cfg) -> None:
        self.started_with.append(cfg)

    async def stop(self) -> None:
        self.stopped += 1


class OwnedManager(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()
        self._env = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)})
        self._env.start()
        _FakeManager.instances.clear()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_no_servers_means_no_manager(self) -> None:
        got = asyncio.run(start_manager_for_workspace(self.ws, factory=_FakeManager))
        self.assertIsNone(got)
        self.assertEqual(_FakeManager.instances, [])

    def test_servers_start_exactly_one_manager(self) -> None:
        p = global_mcp_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"mcpServers": {"echo": {"command": "true"}}}), encoding="utf-8")
        got = asyncio.run(start_manager_for_workspace(self.ws, factory=_FakeManager))
        self.assertIsInstance(got, _FakeManager)
        self.assertEqual(len(_FakeManager.instances), 1)
        self.assertEqual(len(got.started_with), 1)
        self.assertIn("echo", got.started_with[0].servers)


if __name__ == "__main__":
    unittest.main()
