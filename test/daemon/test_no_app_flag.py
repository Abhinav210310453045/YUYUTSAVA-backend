"""The app starts a daemon that serves the API (R-BUG-07).

``daemon.js`` spawned ``yuyutsava daemon --no-ui``. ``--no-ui`` is headless —
no web hub, so no HTTP/WebSocket server — so a daemon the app started or
restarted served nothing the app could reach, while ``daemon.json`` still
advertised a URL. ``--no-app`` keeps the API and only skips opening the app;
``daemon.js`` uses it, and a daemon with no server advertises no URL.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.daemon.options import build_parser
from yuyutsava.daemon.singleton import daemon_discovery_path, write_daemon_discovery

REPO = Path(__file__).resolve().parents[2]


class TheFlags(unittest.TestCase):
    def test_no_app_keeps_the_api_and_no_ui_is_headless(self) -> None:
        args = build_parser().parse_args(["--no-app"])
        self.assertTrue(args.no_app)
        self.assertFalse(args.no_ui)

    def test_the_app_starts_the_daemon_with_no_app(self) -> None:
        spawn = (REPO / "electron-app/src/main/daemon.js").read_text()
        self.assertIn("'daemon', '--no-app'", spawn)
        self.assertNotIn("'--no-ui'", spawn)


class ADaemonWithoutAServerAdvertisesNoUrl(unittest.TestCase):
    def test_discovery_records_none(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": tmp}),
        ):
            write_daemon_discovery(pid=os.getpid(), web_url=None, async_host_url=None)
            self.assertIsNone(json.loads(daemon_discovery_path().read_text())["web_url"])


if __name__ == "__main__":
    unittest.main()
