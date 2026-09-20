"""Reading the active run's thread id, and what happens when there isn't one.

`R-DRY-03`: this was written three times — twice returning `""` outside a run
and once returning `"unknown"` — so the same question had two answers depending
on which subsystem asked. There is one reader now, and the fallback is an
argument rather than a detail of whichever copy you reached.

The fallback matters more than it looks. Callers that pass `"unknown"` are
storing something *under* the thread id, and they would rather file it in one
named bucket than under `""`. The price is that the bucket collides: two
offloads made outside a run are indistinguishable afterwards. That is recorded
here rather than quietly fixed, because which callers should change is a
product decision.
"""

from __future__ import annotations

import unittest
from unittest import mock

from yuyutsava.run_context import thread_id_from_runtime


class InsideARun(unittest.TestCase):
    def test_the_thread_id_is_returned(self) -> None:
        with mock.patch(
            "yuyutsava.run_context.get_config",
            return_value={"configurable": {"thread_id": "th-42"}},
        ):
            self.assertEqual(thread_id_from_runtime(), "th-42")

    def test_the_default_is_not_consulted_when_there_is_an_id(self) -> None:
        with mock.patch(
            "yuyutsava.run_context.get_config",
            return_value={"configurable": {"thread_id": "th-42"}},
        ):
            self.assertEqual(thread_id_from_runtime(default=""), "th-42")


class OutsideARun(unittest.TestCase):
    """Three shapes mean "no thread": no config, no key, and an empty value."""

    SHAPES = ({}, {"configurable": {}}, {"configurable": {"thread_id": ""}}, None)

    def test_the_default_is_returned(self) -> None:
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                with mock.patch("yuyutsava.run_context.get_config", return_value=shape):
                    self.assertEqual(thread_id_from_runtime(), "unknown")
                    self.assertEqual(thread_id_from_runtime(default=""), "")

    def test_a_raising_config_still_answers(self) -> None:
        """Called outside a graph entirely, get_config raises."""
        with mock.patch("yuyutsava.run_context.get_config", side_effect=RuntimeError("no run")):
            self.assertEqual(thread_id_from_runtime(), "unknown")
            self.assertEqual(thread_id_from_runtime(default=""), "")


class TheFallbackIsAChoice(unittest.TestCase):
    def test_two_callers_outside_a_run_share_one_bucket(self) -> None:
        """The cost of "unknown", stated: anything keyed on it collides."""
        with mock.patch("yuyutsava.run_context.get_config", return_value={}):
            first = thread_id_from_runtime()
            second = thread_id_from_runtime()
        self.assertEqual(first, second, "this is the collision, not a bug in the test")

    def test_the_empty_default_is_falsy_so_a_caller_can_refuse(self) -> None:
        """Which is why the compaction and usage paths pass it: they would
        rather do nothing than write under a shared key."""
        with mock.patch("yuyutsava.run_context.get_config", return_value={}):
            self.assertFalse(thread_id_from_runtime(default=""))
            self.assertTrue(thread_id_from_runtime())


class NobodyKeepsTheirOwnCopy(unittest.TestCase):
    def test_one_definition_in_the_tree(self) -> None:
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "yuyutsava"
        found = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in (
                    "thread_id_from_runtime",
                    "_current_thread_id",
                ):
                    found.append(str(path.relative_to(root.parent)))
        self.assertEqual(found, ["yuyutsava/run_context.py"], f"copies: {found}")


if __name__ == "__main__":
    unittest.main()
