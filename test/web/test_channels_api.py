"""GET /channels + enable/disable endpoints over httpx ASGI.

Run:  uv run python -m unittest test.web.test_channels_api -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from test.channels.test_registry import FakeChannelPlugin, make_sink
from yuyutsava.channels.config import ChannelConfig, ChannelsConfig
from yuyutsava.channels.registry import ChannelPluginRegistry
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub


class _NullStore:
    def try_set_proposal_status(self, *a, **kw) -> bool:
        return False


def _broken_factory(params: dict):
    raise ValueError("YUYUTSAVA_TELEGRAM_BOT_TOKEN is not set")


class ChannelsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeChannelPlugin.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg_path = Path(self._tmp.name) / "channels_config.json"
        os.environ["YUYUTSAVA_CHANNELS_CONFIG"] = str(self._cfg_path)

        self.router = ChannelRouter(channels=[])
        self.registry = ChannelPluginRegistry(
            router=self.router,
            sink=make_sink(),
            config=ChannelsConfig(channels={
                "fake": ChannelConfig(name="fake", enabled=False),
            }),
            plugin_factories={
                "fake": FakeChannelPlugin.from_config,
                "broken": _broken_factory,
            },
        )
        app = create_app(
            WebHub(store=_NullStore()), host="127.0.0.1",
            channel_plugins=self.registry,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        os.environ.pop("YUYUTSAVA_CHANNELS_CONFIG", None)
        self._tmp.cleanup()

    async def test_list_channels(self) -> None:
        r = await self.client.get("/channels")
        self.assertEqual(r.status_code, 200)
        rows = {c["name"]: c for c in r.json()["channels"]}
        self.assertIn("fake", rows)
        self.assertFalse(rows["fake"]["running"])
        self.assertTrue(rows["broken"]["available"])

    async def test_enable_disable_roundtrip(self) -> None:
        r = await self.client.post("/channels/fake/enable")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["running"])
        self.assertTrue(body["changed"])
        self.assertTrue(self.registry.is_running("fake"))
        self.assertIsNotNone(self.router.find("fake"))
        # Persisted to channels_config.json.
        raw = json.loads(self._cfg_path.read_text())
        self.assertTrue(raw["channels"]["fake"]["enabled"])

        # Idempotent re-enable.
        r = await self.client.post("/channels/fake/enable")
        self.assertFalse(r.json()["changed"])

        r = await self.client.post("/channels/fake/disable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["changed"])
        self.assertFalse(self.registry.is_running("fake"))
        raw = json.loads(self._cfg_path.read_text())
        self.assertFalse(raw["channels"]["fake"]["enabled"])

    async def test_enable_unknown_404(self) -> None:
        r = await self.client.post("/channels/whatsapp/enable")
        self.assertEqual(r.status_code, 404)

    async def test_enable_misconfigured_422(self) -> None:
        r = await self.client.post("/channels/broken/enable")
        self.assertEqual(r.status_code, 422)
        self.assertIn("BOT_TOKEN", r.json()["message"])
        # Misconfigured plugins must not be persisted as enabled.
        if self._cfg_path.exists():
            raw = json.loads(self._cfg_path.read_text())
            self.assertFalse(raw["channels"].get("broken", {}).get("enabled", False))


if __name__ == "__main__":
    unittest.main()
