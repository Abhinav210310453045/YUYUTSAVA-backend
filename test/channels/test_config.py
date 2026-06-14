"""ChannelsConfig load/save roundtrip.

Run:  uv run python -m unittest test.channels.test_config -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yuyutsava.channels.config import ChannelConfig, ChannelsConfig


class ChannelsConfigTests(unittest.TestCase):
    def test_default_when_missing(self) -> None:
        cfg = ChannelsConfig.from_file(Path("/nonexistent/channels_config.json"))
        self.assertIn("telegram", cfg.channels)
        self.assertFalse(cfg.channels["telegram"].enabled)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels_config.json"
            cfg = ChannelsConfig(channels={
                "telegram": ChannelConfig(
                    name="telegram", enabled=True, params={"poll_timeout_sec": 30},
                ),
            })
            cfg.to_file(path)
            loaded = ChannelsConfig.from_file(path)
            self.assertTrue(loaded.channels["telegram"].enabled)
            self.assertEqual(loaded.channels["telegram"].params,
                             {"poll_timeout_sec": 30})
            # enabled lives next to params in the json body, EventsConfig-style.
            raw = json.loads(path.read_text())
            self.assertEqual(
                raw["channels"]["telegram"],
                {"enabled": True, "poll_timeout_sec": 30},
            )

    def test_with_enabled_preserves_params(self) -> None:
        cfg = ChannelsConfig(channels={
            "telegram": ChannelConfig(
                name="telegram", enabled=False, params={"debounce_sec": 5},
            ),
        })
        flipped = cfg.with_enabled("telegram", True)
        self.assertTrue(flipped.channels["telegram"].enabled)
        self.assertEqual(flipped.channels["telegram"].params, {"debounce_sec": 5})
        # Unknown name creates a fresh disabled-params block.
        added = cfg.with_enabled("whatsapp", True)
        self.assertTrue(added.channels["whatsapp"].enabled)
        self.assertEqual(added.channels["whatsapp"].params, {})

    def test_invalid_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels_config.json"
            path.write_text("{nope")
            with self.assertRaises(RuntimeError):
                ChannelsConfig.from_file(path)


if __name__ == "__main__":
    unittest.main()
