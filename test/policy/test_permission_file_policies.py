"""The policy names ``permissions.json`` accepts, and what happens to the rest.

A name the code does not implement is warned about and read as ``propose``.
``queue_for_user`` and ``refuse_when_no_ui`` used to be accepted silently while
doing exactly that, which told the user they had configured something real.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from yuyutsava.policy.builtin.tool_permissions import PermissionsPolicy


def _load(policy: str) -> tuple[PermissionsPolicy, list[str]]:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "permissions.json"
        path.write_text(json.dumps({"tool_categories": {"tr_*": {"policy": policy}}}))
        with unittest.TestCase().assertLogs(
            "yuyutsava.policy.builtin.tool_permissions", level="DEBUG"
        ) as logs:
            # One debug line so assertLogs never fails on a silent load.
            logging.getLogger("yuyutsava.policy.builtin.tool_permissions").debug("load")
            loaded = PermissionsPolicy.from_file(path)
    return loaded, [r.getMessage() for r in logs.records if r.levelname == "WARNING"]


class ImplementedPolicies(unittest.TestCase):
    def test_load_quietly_and_keep_their_name(self) -> None:
        for name in ("auto_approve", "propose", "AUTO_APPROVE"):
            with self.subTest(name=name):
                loaded, warnings = _load(name)
                self.assertEqual(warnings, [])
                self.assertEqual(loaded.entries[0].policy, name.lower())


class UnimplementedNames(unittest.TestCase):
    def test_are_warned_about_and_read_as_propose(self) -> None:
        for name in ("queue_for_user", "refuse_when_no_ui", "made_up"):
            with self.subTest(name=name):
                loaded, warnings = _load(name)
                self.assertEqual(loaded.entries[0].policy, "propose")
                self.assertEqual(len(warnings), 1)
                self.assertIn(f"unknown policy {name!r}", warnings[0])


if __name__ == "__main__":
    unittest.main()
