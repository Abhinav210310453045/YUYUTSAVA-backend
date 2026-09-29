"""``GET /mcp/status``: every configured MCP server and why one is not up (R-UI-12)."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.mcp.status import CONNECTED, FAILED, ServerStatus


class _Store:
    async def put_event_payload(self, **kw) -> None: ...


class _Manager:
    def status(self, agent_name=None):
        return [
            ServerStatus("gdrive", CONNECTED, tools=128, source="/h/.yuyutsava/mcp_config.json"),
            ServerStatus("notion", FAILED, error="FileNotFoundError: npx"),
        ]


def _get(deps: AppDeps, path: str) -> dict:
    app = create_app(WebHub(store=_Store()), host="127.0.0.1", deps=deps)
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


class McpStatus(unittest.TestCase):
    def test_each_server_with_its_state(self) -> None:
        for path in ("/v1/mcp/status", "/mcp/status"):
            with self.subTest(path=path):
                body = _get(AppDeps(mcp_manager=_Manager()), path)
                self.assertTrue(body["config"].endswith("mcp_config.json"))
                self.assertEqual(
                    [(s["name"], s["state"], s["tools"], s["error"]) for s in body["servers"]],
                    [
                        ("gdrive", "connected", 128, ""),
                        ("notion", "failed", 0, "FileNotFoundError: npx"),
                    ],
                )

    def test_no_manager_lists_no_servers(self) -> None:
        self.assertEqual(_get(AppDeps(), "/v1/mcp/status")["servers"], [])


if __name__ == "__main__":
    unittest.main()
