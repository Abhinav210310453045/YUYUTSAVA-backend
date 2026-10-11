"""``scripts/install.py`` is the one way to install, for the desktop app and the terminal.

What must hold:

* the app's ``.venv`` is synced from the lock, frozen and **inexact** — a plain
  ``uv sync`` uninstalls every optional feature it does not name;
* the terminal command is installed editable under the lock exported as
  constraints, never re-resolved (R-BUG-28);
* both get the product's features by default; ``--ui`` / ``--cli`` pick one;
* no doc tells anyone to run a bare ``uv sync``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import unittest
from pathlib import Path

from test.support.paths import REPO

_spec = importlib.util.spec_from_file_location("install", REPO / "scripts" / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def _printed(*argv: str) -> list[str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        install.main([*argv, "--dry-run"])
    return [line for line in out.getvalue().splitlines() if line.startswith("$ ")]


class TheUi(unittest.TestCase):
    def test_syncs_the_apps_venv_frozen_and_never_strips_it(self) -> None:
        [sync] = install.ui_commands(["voice", "memory"])
        self.assertEqual(sync[:2], ["uv", "sync"])
        self.assertIn("--frozen", sync)
        self.assertIn("--inexact", sync, "an exact sync uninstalls every feature not named")
        extras = [sync[i + 1] for i, a in enumerate(sync) if a == "--extra"]
        self.assertEqual(extras, ["voice", "memory"])


class TheCli(unittest.TestCase):
    def test_exports_the_lock_frozen_and_installs_under_it(self) -> None:
        export, tool = install.cli_commands(["vertex"], Path("/t/c.txt"))
        self.assertEqual(export[:2], ["uv", "export"])
        self.assertIn("--frozen", export)
        self.assertEqual(export[export.index("--extra") + 1], "vertex")
        self.assertEqual(export[export.index("--output-file") + 1], "/t/c.txt")
        self.assertEqual(tool[:3], ["uv", "tool", "install"])
        self.assertIn("--editable", tool)
        self.assertIn(".[vertex]", tool)
        self.assertEqual(tool[tool.index("--constraints") + 1], "/t/c.txt")

    def test_no_extras_installs_the_base_package(self) -> None:
        export, tool = install.cli_commands([], Path("/t/c.txt"))
        self.assertNotIn("--extra", export)
        self.assertIn(".", tool)


class WhatRuns(unittest.TestCase):
    def test_no_flag_installs_both_the_app_first(self) -> None:
        steps = _printed()
        self.assertTrue(steps[0].startswith("$ uv sync"))
        self.assertTrue(any(s.startswith("$ uv tool install") for s in steps))

    def test_ui_and_cli_each_install_only_their_own(self) -> None:
        self.assertEqual([s.split()[2] for s in _printed("--ui")], ["sync"])
        self.assertEqual([s.split()[2] for s in _printed("--cli")], ["export", "tool"])

    def test_the_products_features_by_default_lean_none_and_extra_adds(self) -> None:
        default = _printed("--ui")[0]
        for extra in install.PRODUCT_EXTRAS:
            self.assertIn(f"--extra {extra}", default)
        self.assertNotIn("--extra", _printed("--ui", "--lean")[0])
        self.assertIn("--extra dev", _printed("--ui", "--extra", "dev")[0])


class TheDocs(unittest.TestCase):
    def test_never_tell_anyone_to_run_a_bare_uv_sync(self) -> None:
        bare = re.compile(r"^\s*uv sync\b", re.M)  # a command line to copy; prose may warn
        for doc in ("README.md", "CONTRIBUTING.md"):
            with self.subTest(doc=doc):
                self.assertIsNone(bare.search((REPO / doc).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
