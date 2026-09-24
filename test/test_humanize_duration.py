"""How long something has run, as a short label.

W4 named "one duration humanizer": `cli/async_hitl.py` and
`async_subagents/mirror.py` each held a byte-identical `_fmt_elapsed`, so a
background task's elapsed time was formatted by whichever module happened to
print it. One now, in `yuyutsava.text`.

Two neighbours keep their own shapes on purpose — the sessions table's "2h ago"
answers *when*, and `daemon status` prints a fixed readout — so this pins the
elapsed format and the boundaries where it changes unit.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from yuyutsava.text import humanize_duration

ROOT = pathlib.Path(__file__).resolve().parent.parent / "yuyutsava"


class TheFormat(unittest.TestCase):
    CASES = (
        (0, "0s"),
        (42, "42s"),
        (59.4, "59s"),
        (60, "1m00s"),
        (185, "3m05s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (7627, "2h07m"),  # 2h 7m 7s: seconds dropped past an hour
        (90000, "25h00m"),  # no days: this is how long, not when
    )

    def test_each_boundary(self) -> None:
        for seconds, label in self.CASES:
            with self.subTest(seconds=seconds):
                self.assertEqual(humanize_duration(seconds), label)

    def test_seconds_are_zero_padded_under_an_hour(self) -> None:
        """So a column of them lines up."""
        self.assertEqual(humanize_duration(65), "1m05s")

    def test_minutes_are_zero_padded_over_an_hour(self) -> None:
        self.assertEqual(humanize_duration(3660), "1h01m")


class OneCopy(unittest.TestCase):
    def test_no_module_keeps_its_own(self) -> None:
        found = [
            str(p.relative_to(ROOT.parent))
            for p in sorted(ROOT.rglob("*.py"))
            if re.search(r"^def _fmt_elapsed\b", p.read_text(), re.M)
        ]
        self.assertEqual(found, [])

    def test_both_former_owners_use_it(self) -> None:
        for rel in ("cli/async_hitl.py", "async_subagents/mirror.py"):
            with self.subTest(module=rel):
                self.assertIn("humanize_duration(", (ROOT / rel).read_text())


if __name__ == "__main__":
    unittest.main()
