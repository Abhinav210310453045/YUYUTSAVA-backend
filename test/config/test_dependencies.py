"""The daemon can tell when it runs dependencies the project did not lock (R-BUG-28).

The user's daemon ran from a pipx install that ignored ``uv.lock``: mcp 2.2.0
for a lock of 1.26.0 broke every MCP server, and twelve more coupled packages
were newer than anything the contracts or CI had checked. Every one was inside
its declared range — so the check compares with the lock, not only the ranges.

Run against a scratch checkout (its own pyproject.toml and uv.lock) and the
versions really installed here.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path

from yuyutsava.config.dependencies import check_install

MCP = version("mcp")
_SCRATCH: list[Path] = []


def tearDownModule() -> None:
    for root in _SCRATCH:
        shutil.rmtree(root, ignore_errors=True)


def _checkout(dependencies: list[str], locked: dict[str, str], name: str = "yuyutsava") -> Path:
    root = Path(tempfile.mkdtemp())
    _SCRATCH.append(root)
    deps = ", ".join(f'"{d}"' for d in dependencies)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\ndependencies = [{deps}]\n')
    packages = "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n\n' for n, v in locked.items())
    (root / "uv.lock").write_text(packages)
    return root


class TheInstallCheck(unittest.TestCase):
    def test_a_version_the_lock_does_not_name_is_reported(self) -> None:
        report = check_install(_checkout(["mcp[cli]>=1,<3"], {"mcp": "0.0.1"}))
        self.assertEqual(report.off_lock, (f"mcp {MCP} (lock 0.0.1)",))
        self.assertTrue(report.drifted)

    def test_a_version_outside_its_range_is_reported(self) -> None:
        report = check_install(_checkout(["mcp[cli]<1"], {"mcp": MCP}))
        self.assertEqual(report.out_of_range, (f"mcp {MCP} not in <1",))

    def test_the_locked_install_is_clean_and_lists_its_versions(self) -> None:
        report = check_install(_checkout(["mcp[cli]>=1,<3", "rich>=14"], {"mcp": MCP}))
        self.assertFalse(report.drifted)
        self.assertEqual(report.versions, {"mcp": MCP}, "only coupled dependencies")

    def test_this_checkout_matches_its_own_lock(self) -> None:
        """The repo's own environment is the locked one — what CI runs."""
        report = check_install()
        self.assertFalse(report.drifted, report.out_of_range + report.off_lock)
        self.assertIn("langchain-core", report.versions)


if __name__ == "__main__":
    unittest.main()
