"""Characterization: the `todo_*` tools, per scope.

`R-RDB-02`: `make_todo_tools` is 266 lines — twelve `@tool` closures in one
function — and the set it returns depends on `scope`, so a split has to preserve
two surfaces rather than one.

Order is pinned as well as membership. The bound list is what the model is shown,
and the agent fingerprint sorts tool names before hashing, so a reordering would
pass every other check in the suite.
"""

from __future__ import annotations

import unittest

from yuyutsava.todoboard.tools import make_todo_tools

#: `capture` is what the orchestrator and chat master get: enough to record a
#: card and find one again, and nothing that edits the board.
CAPTURE = ["todo_add", "todo_list", "todo_get", "todo_recall"]

#: `full` is the TinkerAgent's, which works a card rather than filing it.
FULL = [
    *CAPTURE,
    "todo_update",
    "todo_set_status",
    "todo_add_note",
    "todo_add_objective",
    "todo_update_objective",
    "todo_assign_note",
    "todo_attach_artifact",
    "todo_generate_artifact",
]


class TheTodoToolSurface(unittest.TestCase):
    @staticmethod
    def _names(scope: str) -> list[str]:
        # The exchange is never touched at build time; the tools close over it.
        return [t.name for t in make_todo_tools(exchange=object(), scope=scope)]

    def test_capture_scope_can_record_but_not_edit(self) -> None:
        self.assertEqual(self._names("capture"), CAPTURE)

    def test_full_scope_gets_the_whole_board(self) -> None:
        self.assertEqual(self._names("full"), FULL)

    def test_capture_is_a_prefix_of_full(self) -> None:
        """The two scopes are one list truncated, not two hand-kept lists."""
        self.assertEqual(self._names("full")[: len(CAPTURE)], self._names("capture"))

    def test_no_tool_is_bound_twice(self) -> None:
        names = self._names("full")
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_describes_itself(self) -> None:
        for tool in make_todo_tools(exchange=object(), scope="full"):
            with self.subTest(tool=tool.name):
                self.assertTrue((tool.description or "").strip())

    def test_the_author_label_does_not_change_the_surface(self) -> None:
        """`author` labels what the tools write, not which ones exist."""
        master = [t.name for t in make_todo_tools(exchange=object(), scope="full")]
        tinker = [t.name for t in make_todo_tools(exchange=object(), scope="full", author="tinker")]
        self.assertEqual(master, tinker)


if __name__ == "__main__":
    unittest.main()
