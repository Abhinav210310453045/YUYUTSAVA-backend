"""The environment helpers in ``yuyutsava/config/env.py``.

Run:  .venv/bin/python -m pytest test/config/test_env.py
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from yuyutsava.config.env import env, env_flag, env_float, env_int

NAME = "YUYUTSAVA_TEST_ENV_SETTING"
ROLE_NAME = "TESTROLE_YUYUTSAVA_TEST_ENV_SETTING"


def _environ(**values: str) -> mock._patch_dict:
    """Patch os.environ for one test, with both test variables unset unless given."""
    cleared = {k: v for k, v in os.environ.items() if k not in (NAME, ROLE_NAME)}
    return mock.patch.dict(os.environ, {**cleared, **values}, clear=True)


class StringSettings(unittest.TestCase):
    def test_unset_and_blank_both_give_the_default(self) -> None:
        with _environ():
            self.assertEqual(env(NAME, default="d"), "d")
        with _environ(**{NAME: "   "}):
            self.assertEqual(env(NAME, default="d"), "d")

    def test_surrounding_whitespace_is_stripped(self) -> None:
        with _environ(**{NAME: "  value  "}):
            self.assertEqual(env(NAME), "value")

    def test_the_role_override_wins_when_it_has_a_value(self) -> None:
        with _environ(**{NAME: "base", ROLE_NAME: "role"}):
            self.assertEqual(env(NAME, "testrole"), "role")
            self.assertEqual(env(NAME), "base")

    def test_a_blank_role_override_falls_back_to_the_plain_name(self) -> None:
        with _environ(**{NAME: "base", ROLE_NAME: "  "}):
            self.assertEqual(env(NAME, "testrole"), "base")


class NumberSettings(unittest.TestCase):
    def test_integers(self) -> None:
        cases = {" 42 ": 42, "-3": -3, "4.2": 7, "abc": 7, "": 7}
        for value, expected in cases.items():
            with self.subTest(value=value), _environ(**{NAME: value}):
                self.assertEqual(env_int(NAME, default=7), expected)
        with _environ():
            self.assertEqual(env_int(NAME, default=7), 7)

    def test_floats(self) -> None:
        cases = {" 0.5 ": 0.5, "2": 2.0, "x": 1.5, "": 1.5}
        for value, expected in cases.items():
            with self.subTest(value=value), _environ(**{NAME: value}):
                self.assertEqual(env_float(NAME, default=1.5), expected)

    def test_numbers_honour_the_role_override(self) -> None:
        with _environ(**{NAME: "1", ROLE_NAME: "2"}):
            self.assertEqual(env_int(NAME, "testrole", default=0), 2)
            self.assertEqual(env_float(NAME, "testrole", default=0.0), 2.0)


class FlagSettings(unittest.TestCase):
    def test_a_true_vocabulary_turns_on_only_for_its_values(self) -> None:
        on = ("1", "true", "yes")
        cases = {"yes": True, "TRUE": True, " 1 ": True, "on": False, "maybe": False}
        for value, expected in cases.items():
            with self.subTest(value=value), _environ(**{NAME: value}):
                self.assertIs(env_flag(NAME, default=False, true=on), expected)

    def test_a_false_vocabulary_turns_off_only_for_its_values(self) -> None:
        off = ("0", "false", "no", "off")
        cases = {"0": False, "OFF": False, "no": False, "1": True, "anything": True}
        for value, expected in cases.items():
            with self.subTest(value=value), _environ(**{NAME: value}):
                self.assertIs(env_flag(NAME, default=True, false=off), expected)

    def test_unset_and_blank_give_the_default_in_both_modes(self) -> None:
        for environ in ({}, {NAME: ""}, {NAME: "  "}):
            with self.subTest(environ=environ), _environ(**environ):
                self.assertIs(env_flag(NAME, default=True, true=("1",)), True)
                self.assertIs(env_flag(NAME, default=False, false=("0",)), False)

    def test_flags_honour_the_role_override(self) -> None:
        with _environ(**{NAME: "0", ROLE_NAME: "1"}):
            self.assertIs(env_flag(NAME, "testrole", true=("1",)), True)

    def test_exactly_one_vocabulary_is_required(self) -> None:
        with self.assertRaises(ValueError):
            env_flag(NAME)
        with self.assertRaises(ValueError):
            env_flag(NAME, true=("1",), false=("0",))


if __name__ == "__main__":
    unittest.main()
