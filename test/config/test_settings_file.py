"""``settings.toml`` is the other place a setting can live, and it is safe to write.

What must hold:

* the order is *shell > settings.toml > .env > the default*, which is what lets
  a one-off override and a CI run keep working;
* no reader had to change for that, so applying it is putting values into the
  environment and nothing more;
* a write is whole or not at all, and a change made elsewhere meanwhile is
  refused rather than lost;
* a person can keep more than one setup and go back to one, always.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from yuyutsava.config.settings_file import (
    ABSENT,
    SettingsError,
    SettingsStore,
    StaleWrite,
    apply_settings,
    parse,
    read_settings,
    shell_keys,
)


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name)
        self.store = SettingsStore(self.home / "settings.toml", self.home / "settings-versions")


class TheFile(_Store):
    def test_it_groups_settings_the_way_the_catalog_groups_them(self) -> None:
        self.store.set({"TAVILY_API_KEY": "tvly-1", "YUYUTSAVA_KROKI_URL": "http://here:8000"})
        text = self.store.path.read_text(encoding="utf-8")
        self.assertIn("[search]", text)
        self.assertIn("[diagrams]", text)
        self.assertIn("version = 1", text)

    def test_a_setting_is_changed_without_disturbing_the_others(self) -> None:
        self.store.set({"TAVILY_API_KEY": "one", "EXA_API_KEY": "two"})
        self.store.set({"TAVILY_API_KEY": "three"})
        self.assertEqual(
            self.store.settings().values, {"TAVILY_API_KEY": "three", "EXA_API_KEY": "two"}
        )

    def test_an_empty_value_removes_a_setting(self) -> None:
        self.store.set({"TAVILY_API_KEY": "one", "EXA_API_KEY": "two"})
        self.store.unset(["TAVILY_API_KEY"])
        self.assertEqual(self.store.settings().values, {"EXA_API_KEY": "two"})

    def test_no_file_is_no_settings_rather_than_an_error(self) -> None:
        self.assertEqual(self.store.read().digest, ABSENT)
        self.assertEqual(self.store.settings().values, {})

    def test_a_name_the_catalog_does_not_know_is_reported_not_dropped(self) -> None:
        self.store.path.write_text('[other]\nSOMETHING_NEW = "1"\n', encoding="utf-8")
        found = self.store.settings()
        self.assertEqual(found.values, {"SOMETHING_NEW": "1"})
        self.assertEqual(found.unknown, ("SOMETHING_NEW",), "a newer build's setting, not a typo")

    def test_a_broken_file_says_where_it_is_wrong(self) -> None:
        with self.assertRaises(SettingsError) as caught:
            parse("[search\n", source="settings.toml")
        self.assertIn("settings.toml", str(caught.exception))


class WritingIsWholeOrNotAtAll(_Store):
    def test_a_change_made_elsewhere_meanwhile_is_refused(self) -> None:
        self.store.set({"TAVILY_API_KEY": "one"})
        stale = self.store.read().digest
        self.store.set({"EXA_API_KEY": "two"})  # somebody else
        with self.assertRaises(StaleWrite):
            self.store.write({"TAVILY_API_KEY": "three"}, expected=stale)
        self.assertIn("EXA_API_KEY", self.store.settings().values, "nothing was lost")


class ThePrecedence(unittest.TestCase):
    """Shell > settings.toml > .env > the default, in that order and no other."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "settings.toml"
        SettingsStore(self.path).set(
            {"TAVILY_API_KEY": "from-settings", "EXA_API_KEY": "from-settings"}
        )

    def test_the_shell_wins_even_over_a_dotenv_loaded_after_the_snapshot(self) -> None:
        environ = {"TAVILY_API_KEY": "from-shell"}
        shell = shell_keys(environ)
        environ["EXA_API_KEY"] = "from-dotenv"  # load_dotenv(override=True) does this
        apply_settings(environ, path=self.path, protect=shell)
        self.assertEqual(environ["TAVILY_API_KEY"], "from-shell", "a shell override must survive")
        self.assertEqual(environ["EXA_API_KEY"], "from-settings", "the file beats a .env")

    def test_without_a_snapshot_anything_already_set_is_left_alone(self) -> None:
        environ = {"TAVILY_API_KEY": "already"}
        apply_settings(environ, path=self.path)
        self.assertEqual(environ["TAVILY_API_KEY"], "already")

    def test_applying_is_all_it_does_so_no_reader_had_to_change(self) -> None:
        environ: dict[str, str] = {}
        applied = apply_settings(environ, path=self.path)
        self.assertEqual(sorted(applied), ["EXA_API_KEY", "TAVILY_API_KEY"])
        self.assertEqual(environ["TAVILY_API_KEY"], "from-settings")

    def test_a_file_that_does_not_load_never_stops_a_start_up(self) -> None:
        self.path.write_text("[search\n", encoding="utf-8")
        environ: dict[str, str] = {}
        self.assertEqual(apply_settings(environ, path=self.path), [])
        self.assertEqual(environ, {}, "`yuyutsava config check` says what is wrong, not a crash")

    def test_a_missing_file_reads_as_nothing(self) -> None:
        self.assertEqual(read_settings(self.path.with_name("absent.toml")).values, {})


class KeepingMoreThanOne(_Store):
    def test_a_version_can_be_kept_and_come_back_to(self) -> None:
        self.store.set({"TAVILY_API_KEY": "work"})
        self.store.save_version("work")
        self.store.set({"TAVILY_API_KEY": "home"})
        self.store.use_version("work")
        self.assertEqual(self.store.settings().values["TAVILY_API_KEY"], "work")

    def test_going_back_is_itself_undoable(self) -> None:
        self.store.set({"TAVILY_API_KEY": "work"})
        self.store.save_version("work")
        self.store.set({"TAVILY_API_KEY": "home"})
        self.store.use_version("work")
        self.assertIn("previous", self.store.versions())
        self.store.use_version("previous")
        self.assertEqual(self.store.settings().values["TAVILY_API_KEY"], "home")

    def test_a_version_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(SettingsError) as caught:
            self.store.use_version("nothing-like-this")
        self.assertIn("nothing-like-this", str(caught.exception))

    def test_a_name_that_could_escape_the_directory_is_refused(self) -> None:
        for name in ("../escape", "a/b", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.version_path(name)

    def test_there_is_nothing_to_keep_before_anything_is_written(self) -> None:
        with self.assertRaises(SettingsError):
            self.store.save_version("empty")


if __name__ == "__main__":
    unittest.main()
