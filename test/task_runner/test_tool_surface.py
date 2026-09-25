"""Characterization: the tool surface `bind_tools` hands the agent.

`R-RDB-02`: `bind_tools` is 609 lines — twelve `@tool` closures in one function
— and nothing tested what it returns. The agent fingerprint pins the tool
*names* per profile, which would catch a rename, but not a changed signature, a
lost description, or a tool that stops being bound at all.

Pinned before the function is split by family, so the split can be shown to
preserve the surface rather than argued to.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import bind_tools

# Every tool the TaskRunner binds, and the arguments each takes. The argument
# names are part of the contract: the model calls these by keyword, so a rename
# is a breaking change to every prompt and saved skill that mentions them.
EXPECTED: dict[str, list[str]] = {
    "tr_ask_user": ["options", "question"],
    "tr_delete_file": ["path", "reason"],
    "tr_execute": ["command", "elevated", "reason", "timeout"],
    "tr_execute_in_sandbox": ["command", "reason", "timeout"],
    "tr_fetch_url": ["dest_path", "expected_type", "reason", "timeout", "url"],
    "tr_glob": ["max_entries", "path", "pattern", "reason"],
    "tr_grep": [
        "case_insensitive",
        "context_lines",
        "max_matches",
        "path",
        "pattern",
        "reason",
    ],
    "tr_ls": ["max_entries", "path", "reason"],
    "tr_read_file": ["limit", "offset", "path", "reason"],
    "tr_run_python": ["reason", "script_path", "timeout"],
    "tr_sysinfo": ["reason"],
    "tr_write_file": ["append", "content", "path", "reason"],
}


class TheBoundToolSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.tools = bind_tools(TaskRunnerAgent(root, sandbox_root=(root / "_sandbox").resolve()))
        cls.by_name = {t.name: t for t in cls.tools}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_tool_is_bound_and_no_others(self) -> None:
        self.assertEqual(set(self.by_name), set(EXPECTED))
        # No duplicates — a name bound twice would shadow silently.
        self.assertEqual(len(self.tools), len(EXPECTED))

    def test_each_tool_describes_itself(self) -> None:
        """The description is the model's only instruction for a tool."""
        for name, tool in sorted(self.by_name.items()):
            with self.subTest(tool=name):
                self.assertTrue((tool.description or "").strip(), f"{name} has no description")

    def test_each_tool_keeps_its_arguments(self) -> None:
        for name, expected_args in sorted(EXPECTED.items()):
            with self.subTest(tool=name):
                self.assertEqual(
                    sorted(self.by_name[name].args),
                    expected_args,
                    f"{name} arguments changed — the model calls these by keyword",
                )

    def test_every_operation_records_why_it_ran(self) -> None:
        """`reason` is universal, and it is not decoration.

        It is what the permission prompt shows the user and what the audit log
        keeps, so a tool that stopped asking for one would quietly make both
        useless. Only `tr_ask_user` is exempt — it *is* the prompt.
        """
        for name, tool in sorted(self.by_name.items()):
            if name == "tr_ask_user":
                continue
            with self.subTest(tool=name):
                self.assertIn("reason", tool.args)

    def test_binding_twice_yields_the_same_surface(self) -> None:
        """Rebinding the same kind of gateway must not drift."""
        again = bind_tools(TaskRunnerAgent(Path(self._tmp.name)))
        self.assertEqual(sorted(t.name for t in again), sorted(self.by_name))


if __name__ == "__main__":
    unittest.main()
