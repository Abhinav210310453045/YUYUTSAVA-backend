"""``scripts/install_cli.py`` installs the command under the lock (R-BUG-28).

A PATH install that ignored ``uv.lock`` is how the daemon came to run mcp 2.2.0
for a lock of 1.26.0. What must hold: the lock is exported frozen (never
re-resolved), and the tool is installed editable under it as constraints, with
the chosen extras on both sides.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from test.support.paths import REPO

_spec = importlib.util.spec_from_file_location("install_cli", REPO / "scripts" / "install_cli.py")
install_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_cli)


class TheCommands(unittest.TestCase):
    def test_the_lock_is_exported_frozen_and_installed_as_constraints(self) -> None:
        export, install = install_cli.commands(["vertex"], Path("/t/c.txt"))
        self.assertEqual(export[:2], ["uv", "export"])
        self.assertIn("--frozen", export)
        self.assertEqual(export[export.index("--extra") + 1], "vertex")
        self.assertEqual(export[export.index("--output-file") + 1], "/t/c.txt")
        self.assertEqual(install[:3], ["uv", "tool", "install"])
        self.assertIn("--editable", install)
        self.assertIn(".[vertex]", install)
        self.assertEqual(install[install.index("--constraints") + 1], "/t/c.txt")

    def test_no_extras_installs_the_base_package(self) -> None:
        export, install = install_cli.commands([], Path("/t/c.txt"))
        self.assertNotIn("--extra", export)
        self.assertIn(".", install)


if __name__ == "__main__":
    unittest.main()
