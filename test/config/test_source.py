"""Settings live in one of two files, and a person may change their mind at any time.

What must hold:

* a ``.env`` is something a person edits, so a change keeps their comments,
  their ordering and their other variables exactly as they were;
* moving settings between the two files never loses one, and never leaves them
  in neither;
* which file is in force is the files themselves, not a flag that can disagree
  with them;
* where both files set the same name, that is said out loud, because one of
  them is being ignored.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from yuyutsava.config.env_file import ADDED_HEADING, read_env_file, write_env_values
from yuyutsava.config.source import (
    ENV,
    SETTINGS,
    active_source,
    env_source,
    move_settings,
    settings_source,
)


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name)
        patcher = patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TheEnvFile(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / ".env"

    def test_a_persons_own_lines_survive_a_change(self) -> None:
        self.path.write_text(
            "# why this is here\nFOO=1\n\n# and this\nTAVILY_API_KEY=old\nBAR=2\n", encoding="utf-8"
        )
        write_env_values(self.path, {"TAVILY_API_KEY": "new"}, environ={})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# why this is here", text)
        self.assertIn("# and this", text)
        self.assertIn("TAVILY_API_KEY=new", text)
        self.assertEqual(read_env_file(self.path)["BAR"], "2", "another variable is untouched")

    def test_a_variable_is_changed_where_it_already_stands(self) -> None:
        self.path.write_text("A=1\nTAVILY_API_KEY=old\nB=2\n", encoding="utf-8")
        write_env_values(self.path, {"TAVILY_API_KEY": "new"}, environ={})
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(lines, ["A=1", "TAVILY_API_KEY=new", "B=2"], "ordering is the person's")

    def test_a_new_variable_says_where_it_came_from(self) -> None:
        self.path.write_text("A=1\n", encoding="utf-8")
        write_env_values(self.path, {"EXA_API_KEY": "e"}, environ={})
        self.assertIn(ADDED_HEADING, self.path.read_text(encoding="utf-8"))

    def test_removing_the_last_added_line_takes_our_heading_with_it(self) -> None:
        # Otherwise a person is left with a heading for a section that has
        # nothing in it, and nothing would ever clean it up.
        theirs = "# mine\nWAKE_WORDS=hey_jarvis\n"
        self.path.write_text(theirs, encoding="utf-8")
        write_env_values(self.path, {"EXA_API_KEY": "e"}, environ={})
        self.assertIn(ADDED_HEADING, self.path.read_text(encoding="utf-8"))
        write_env_values(self.path, {"EXA_API_KEY": ""}, environ={})
        self.assertEqual(self.path.read_text(encoding="utf-8"), theirs)

    def test_the_heading_stays_while_anything_is_still_under_it(self) -> None:
        write_env_values(self.path, {"EXA_API_KEY": "e", "TAVILY_API_KEY": "t"}, environ={})
        write_env_values(self.path, {"EXA_API_KEY": ""}, environ={})
        self.assertIn(ADDED_HEADING, self.path.read_text(encoding="utf-8"))
        self.assertEqual(read_env_file(self.path), {"TAVILY_API_KEY": "t"})

    def test_an_empty_value_removes_the_line_and_the_variable(self) -> None:
        self.path.write_text("A=1\nB=2\n", encoding="utf-8")
        environ = {"A": "1", "B": "2"}
        write_env_values(self.path, {"A": ""}, environ=environ)
        self.assertEqual(read_env_file(self.path), {"B": "2"})
        self.assertNotIn("A", environ, "it was loaded from there, so it leaves there too")

    def test_a_value_that_needs_quoting_gets_it(self) -> None:
        write_env_values(self.path, {"A": "has spaces", "B": "plain"}, environ={})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('A="has spaces"', text)
        self.assertIn("B=plain", text)
        self.assertEqual(read_env_file(self.path)["A"], "has spaces")

    def test_writing_to_a_file_that_is_not_there_yet_works(self) -> None:
        write_env_values(self.path, {"A": "1"}, environ={})
        self.assertEqual(read_env_file(self.path), {"A": "1"})


class WhichFileIsInForce(_Home):
    def test_with_neither_it_is_the_one_that_can_be_checked(self) -> None:
        self.assertEqual(active_source().kind, SETTINGS)

    def test_a_dotenv_on_its_own_is_respected(self) -> None:
        env_source().write({"TAVILY_API_KEY": "t"})
        self.assertEqual(active_source().kind, ENV)

    def test_settings_toml_wins_when_both_are_there(self) -> None:
        env_source().write({"TAVILY_API_KEY": "from-env"})
        settings_source().write({"TAVILY_API_KEY": "from-settings"})
        self.assertEqual(active_source().kind, SETTINGS, "this is what start-up does too")

    def test_a_name_set_in_both_is_named_so_nobody_wonders(self) -> None:
        env_source().write({"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"})
        settings_source().write({"TAVILY_API_KEY": "c"})
        self.assertEqual(settings_source().overlaps(env_source()), ["TAVILY_API_KEY"])


class MovingBetweenThem(_Home):
    def test_everything_moves_and_nothing_is_left_behind(self) -> None:
        env_source().write({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"})
        target, moved = move_settings(SETTINGS)
        self.assertEqual(moved, ["EXA_API_KEY", "TAVILY_API_KEY"])
        self.assertEqual(target.values(), {"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"})
        self.assertEqual(env_source().values(), {})

    def test_it_goes_back_the_same_way(self) -> None:
        settings_source().write({"TAVILY_API_KEY": "t"})
        target, moved = move_settings(ENV)
        self.assertEqual(moved, ["TAVILY_API_KEY"])
        self.assertEqual(target.values()["TAVILY_API_KEY"], "t")
        self.assertEqual(settings_source().values(), {})

    def test_a_copy_can_be_left_where_they_were(self) -> None:
        env_source().write({"TAVILY_API_KEY": "t"})
        move_settings(SETTINGS, keep=True)
        self.assertEqual(env_source().values()["TAVILY_API_KEY"], "t")
        self.assertEqual(settings_source().values()["TAVILY_API_KEY"], "t")

    def test_moving_nothing_is_not_an_error(self) -> None:
        target, moved = move_settings(SETTINGS)
        self.assertEqual(moved, [])
        self.assertFalse(target.exists)

    def test_somewhere_else_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            move_settings("somewhere-else")


if __name__ == "__main__":
    unittest.main()
