"""Every default for the daemon's address is the one constant (R-BUG-04).

The remote-attach client defaulted to port 8765 while the daemon listens on
7654 by default. No caller relied on the client's default, so it was dead —
and wrong the moment anyone did. Now there is one constant and the client has
no default at all.
"""

from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

from yuyutsava.cli.commands import attach
from yuyutsava.cli.remote_attach import CliAttachClient
from yuyutsava.config.network import DEFAULT_DAEMON_PORT, DEFAULT_DAEMON_URL
from yuyutsava.daemon.config import DaemonConfig
from yuyutsava.daemon.web.services.config_schema import config_groups


class OneDefault(unittest.TestCase):
    def test_the_daemon_listens_on_it(self) -> None:
        self.assertEqual(DaemonConfig().web_port, DEFAULT_DAEMON_PORT)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YUYUTSAVA_DAEMON_PORT", None)
            self.assertEqual(DaemonConfig.from_env().web_port, DEFAULT_DAEMON_PORT)

    def test_attach_falls_back_to_it(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(attach, "read_daemon_discovery", return_value=None),
        ):
            os.environ.pop("YUYUTSAVA_DAEMON_URL", None)
            self.assertEqual(attach._default_daemon_url(), DEFAULT_DAEMON_URL)

    def test_the_settings_catalog_shows_it(self) -> None:
        port_var = next(
            v for g in config_groups() for v in g.vars if v.key == "YUYUTSAVA_DAEMON_PORT"
        )
        self.assertEqual(port_var.default, str(DEFAULT_DAEMON_PORT))

    def test_the_attach_client_has_no_default_to_get_wrong(self) -> None:
        base_url = inspect.signature(CliAttachClient).parameters["base_url"]
        self.assertIs(base_url.default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
