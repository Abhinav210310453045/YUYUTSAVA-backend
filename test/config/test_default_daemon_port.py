"""The app and the daemon agree on the default daemon port.

The port is defined twice, once per language: yuyutsava/config/network.py for
the daemon and the CLI, electron-app/src/main/config.js for the app. If they
drift, an app with no port configured looks for a daemon that is not there.

Run:  .venv/bin/python -m pytest test/config/test_default_daemon_port.py
"""

from __future__ import annotations

import re
import unittest

from test.support.paths import REPO
from yuyutsava.config.network import DEFAULT_DAEMON_PORT

APP_CONFIG = REPO / "electron-app" / "src" / "main" / "config.js"
RENDERER_CLIENT = REPO / "electron-app" / "src" / "renderer" / "api" / "client.js"


class TheDefaultPortAgrees(unittest.TestCase):
    def test_the_app_main_process(self) -> None:
        m = re.search(r"const DEFAULT_DAEMON_PORT = (\d+)", APP_CONFIG.read_text())
        self.assertIsNotNone(m, "DEFAULT_DAEMON_PORT not found in main/config.js")
        self.assertEqual(int(m.group(1)), DEFAULT_DAEMON_PORT)

    def test_the_renderers_fallback(self) -> None:
        m = re.search(r"let _base = 'http://127\.0\.0\.1:(\d+)'", RENDERER_CLIENT.read_text())
        self.assertIsNotNone(m, "the renderer's fallback base URL moved")
        self.assertEqual(int(m.group(1)), DEFAULT_DAEMON_PORT)


if __name__ == "__main__":
    unittest.main()
