"""A file replaced through ``platform.files`` is whole, and a private one is owner-only, on every OS.

Run:  .venv/bin/python -m pytest test/platform/test_files.py
"""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from yuyutsava.platform.files import replace_private_text, replace_text


def _temporaries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def _windows_grantees(path: Path) -> list[str]:
    """The accounts named in *path*'s ACL, one per access-control entry."""
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    entries = [line.strip() for line in out.splitlines()[:-1] if line.strip()]
    entries[0] = entries[0][len(str(path)) :].strip()
    return [entry.split(":")[0] for entry in entries if ":" in entry]


class AReplacedFileIsWhole(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_contents_replace_the_old_and_nothing_is_left_behind(self) -> None:
        target = self.dir / "models.toml"
        target.write_text("old = 1\n", encoding="utf-8")
        replace_text(target, "new = 2  # é\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new = 2  # é\n")
        self.assertEqual(_temporaries(self.dir), [])

    def test_a_missing_directory_is_created(self) -> None:
        target = self.dir / "nested" / "models.toml"
        replace_text(target, "a = 1\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "a = 1\n")

    def test_a_failed_write_leaves_the_target_untouched_and_no_temporary(self) -> None:
        target = self.dir / "models.toml"
        target.write_text("kept = true\n", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            replace_text(target, "\udc80")  # a lone surrogate cannot be encoded
        self.assertEqual(target.read_text(encoding="utf-8"), "kept = true\n")
        self.assertEqual(_temporaries(self.dir), [])


class APrivateFileIsOwnerOnly(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "secrets.toml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_the_owner_can_read_it(self) -> None:
        replace_private_text(self.target, 'openrouter = "sk-or-test"\n')
        self.assertEqual(self.target.read_text(encoding="utf-8"), 'openrouter = "sk-or-test"\n')
        if sys.platform == "win32":
            self.assertEqual(len(_windows_grantees(self.target)), 1, _windows_grantees(self.target))
        else:
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_replacing_a_shared_file_makes_it_private(self) -> None:
        replace_text(self.target, "a = 1\n")
        replace_private_text(self.target, "a = 2\n")
        if sys.platform == "win32":
            self.assertEqual(len(_windows_grantees(self.target)), 1)
        else:
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)


class TheCheckFails(unittest.TestCase):
    """Negative control: an ordinary replace is not mistaken for a private one."""

    def test_a_shared_file_is_readable_by_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shared.toml"
            replace_text(target, "a = 1\n")
            if sys.platform == "win32":
                self.assertGreater(len(_windows_grantees(target)), 1)
            else:
                mode = stat.S_IMODE(target.stat().st_mode)
                if mode == 0o600:
                    self.skipTest("a umask of 077 makes every new file owner-only")
                self.assertNotEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
