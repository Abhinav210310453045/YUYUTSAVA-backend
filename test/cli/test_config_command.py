"""``yuyutsava config`` is the terminal's half of everything that is not a model.

What must hold:

* a value is read from the files, not from this process's environment, because
  ``yuyutsava config`` is not the chat path and nothing applied them here;
* a secret is never printed, only that it is set and how long it is;
* a name the catalog does not know is refused by name, with what to run to see
  the real ones;
* where a value comes from is always said, because two files can hold settings
  and a person needs to know which one they are editing.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from yuyutsava.cli.commands.config import resolved, run_config
from yuyutsava.config.source import env_source, settings_source


class _Config(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name)
        patcher = patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("TAVILY_API_KEY", "EXA_API_KEY", "YUYUTSAVA_KROKI_URL"):
            os.environ.pop(name, None)

    def run_it(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_config(list(argv))
        return code, out.getvalue() + err.getvalue()


class ReadingASetting(_Config):
    def test_a_value_just_written_is_the_value_reported(self) -> None:
        # Nothing has applied settings.toml into this process, so reading the
        # environment would have said "not set" about a value on disk.
        self.run_it("set", "YUYUTSAVA_KROKI_URL", "http://here:8000")
        code, printed = self.run_it("get", "YUYUTSAVA_KROKI_URL")
        self.assertEqual(code, 0)
        self.assertIn("http://here:8000", printed)

    def test_it_says_which_file_the_value_came_from(self) -> None:
        self.run_it("set", "YUYUTSAVA_KROKI_URL", "http://here:8000")
        _, printed = self.run_it("get", "YUYUTSAVA_KROKI_URL")
        self.assertIn(str(self.home / "settings.toml"), printed)

    def test_a_default_is_shown_as_a_default(self) -> None:
        _, printed = self.run_it("get", "YUYUTSAVA_KROKI_URL")
        self.assertIn("http://localhost:8000", printed)
        self.assertIn("the default", printed)

    def test_a_secret_is_never_printed(self) -> None:
        self.run_it("set", "TAVILY_API_KEY", "tvly-super-secret")
        for argv in (("get", "TAVILY_API_KEY"), ("list", "--group", "search")):
            with self.subTest(argv=argv):
                _, printed = self.run_it(*argv)
                self.assertNotIn("tvly-super-secret", printed)
                self.assertIn("set, 17 characters", printed)

    def test_a_name_it_does_not_know_says_what_to_run(self) -> None:
        code, printed = self.run_it("get", "NOT_A_SETTING")
        self.assertEqual(code, 1)
        self.assertIn("NOT_A_SETTING", printed)
        self.assertIn("yuyutsava config list", printed)

    def test_a_group_that_is_not_there_is_refused(self) -> None:
        code, printed = self.run_it("list", "--group", "nothing-like-this")
        self.assertEqual(code, 1)
        self.assertIn("nothing-like-this", printed)


class WhereTheValueComesFrom(_Config):
    def test_the_shell_is_named_when_it_is_the_shell(self) -> None:
        with patch.dict(os.environ, {"YUYUTSAVA_KROKI_URL": "http://from-shell:8000"}):
            value, where = resolved("YUYUTSAVA_KROKI_URL")
        self.assertEqual(value, "http://from-shell:8000")
        self.assertEqual(where, "your shell")

    def test_settings_toml_beats_the_dotenv_as_start_up_does(self) -> None:
        env_source().write({"TAVILY_API_KEY": "from-env"})
        settings_source().write({"TAVILY_API_KEY": "from-settings"})
        value, where = resolved("TAVILY_API_KEY")
        self.assertEqual(value, "from-settings")
        self.assertEqual(where, str(self.home / "settings.toml"))

    def test_a_shell_value_matching_a_file_is_credited_to_the_file(self) -> None:
        settings_source().write({"TAVILY_API_KEY": "same"})
        with patch.dict(os.environ, {"TAVILY_API_KEY": "same"}):
            _, where = resolved("TAVILY_API_KEY")
        self.assertEqual(where, str(self.home / "settings.toml"), "the file is what a person edits")

    def test_nothing_set_anywhere_is_nowhere(self) -> None:
        self.assertEqual(resolved("TAVILY_API_KEY"), ("", ""))


class ChangingASetting(_Config):
    def test_set_then_unset_leaves_nothing_behind(self) -> None:
        self.run_it("set", "TAVILY_API_KEY", "tvly-1")
        self.assertIn("TAVILY_API_KEY", settings_source().values())
        code, printed = self.run_it("unset", "TAVILY_API_KEY")
        self.assertEqual(code, 0)
        self.assertNotIn("TAVILY_API_KEY", settings_source().values())
        self.assertIn("removed", printed)

    def test_unsetting_what_was_never_set_is_not_a_failure(self) -> None:
        code, printed = self.run_it("unset", "TAVILY_API_KEY")
        self.assertEqual(code, 0)
        self.assertIn("is not set", printed)

    def test_setting_a_name_it_does_not_know_is_refused(self) -> None:
        code, _ = self.run_it("set", "NOT_A_SETTING", "x")
        self.assertEqual(code, 1)
        self.assertFalse(settings_source().exists, "nothing was written")


class MovingBetweenFiles(_Config):
    def test_a_move_reports_every_name_it_moved(self) -> None:
        env_source().write({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"})
        code, printed = self.run_it("move", "--to", "settings")
        self.assertEqual(code, 0)
        self.assertIn("TAVILY_API_KEY", printed)
        self.assertIn("EXA_API_KEY", printed)
        self.assertEqual(env_source().values(), {})

    def test_moving_nothing_says_so_rather_than_failing(self) -> None:
        code, printed = self.run_it("move", "--to", "env")
        self.assertEqual(code, 0)
        self.assertIn("nothing to move", printed)

    def test_where_names_a_variable_that_two_files_disagree_about(self) -> None:
        env_source().write({"TAVILY_API_KEY": "from-env"})
        settings_source().write({"TAVILY_API_KEY": "from-settings"})
        _, printed = self.run_it("where")
        self.assertIn("TAVILY_API_KEY", printed)
        self.assertIn("ignored", printed)


class KeepingVersions(_Config):
    def test_save_list_use_and_remove(self) -> None:
        self.run_it("set", "TAVILY_API_KEY", "work")
        self.assertEqual(self.run_it("version", "save", "work")[0], 0)
        self.assertIn("work", self.run_it("version", "list")[1])
        self.run_it("set", "TAVILY_API_KEY", "home")
        self.assertEqual(self.run_it("version", "use", "work")[0], 0)
        self.assertEqual(settings_source().values()["TAVILY_API_KEY"], "work")
        self.assertEqual(self.run_it("version", "remove", "work")[0], 0)

    def test_a_version_that_is_not_there_is_refused_by_name(self) -> None:
        code, printed = self.run_it("version", "use", "nothing-like-this")
        self.assertEqual(code, 1)
        self.assertIn("nothing-like-this", printed)

    def test_a_version_needs_a_name(self) -> None:
        self.assertEqual(self.run_it("version", "save")[0], 2)

    def test_no_versions_yet_says_how_to_keep_one(self) -> None:
        _, printed = self.run_it("version", "list")
        self.assertIn("yuyutsava config version save", printed)


class Checking(_Config):
    def test_a_setting_with_nothing_to_check_says_so(self) -> None:
        code, printed = self.run_it("check", "YUYUTSAVA_HOME")
        self.assertEqual(code, 0)
        self.assertIn("nothing to check", printed)

    def test_the_ones_that_cost_are_skipped_unless_live_is_asked_for(self) -> None:
        _, printed = self.run_it("check", "TAVILY_API_KEY")
        self.assertIn("--live", printed)


class NoSubcommand(_Config):
    def test_it_prints_help_rather_than_guessing(self) -> None:
        code, printed = self.run_it()
        self.assertEqual(code, 2)
        self.assertIn("usage", printed.lower())


if __name__ == "__main__":
    unittest.main()
