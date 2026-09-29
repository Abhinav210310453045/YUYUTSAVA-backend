"""The daemon's ready summary names every MCP server, failed ones included (R-UI-12).

It listed only the servers that started, so a failed one showed up as a
traceback further up the log and nowhere in the summary a user reads.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.daemon.bootstrap import build_mcp


class TheReadyLine(unittest.TestCase):
    def test_a_failed_server_is_in_the_summary_with_its_cause(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ws:
            config = {"mcpServers": {"notion": {"command": "/nonexistent/notion-mcp"}}}
            Path(home, "mcp_config.json").write_text(json.dumps(config))

            async def go():
                manager = await build_mcp(Path(ws))
                await manager.stop()

            with (
                mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": home}),
                self.assertLogs("yuyutsava.daemon.bootstrap", "INFO") as logs,
                self.assertLogs("yuyutsava.mcp.loader", "ERROR"),
            ):
                asyncio.run(asyncio.wait_for(go(), 30))
        line = next(r for r in logs.output if "mcp       :" in r)
        self.assertIn("notion — failed: FileNotFoundError", line)


if __name__ == "__main__":
    unittest.main()
