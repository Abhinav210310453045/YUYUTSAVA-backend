"""A chat's start shows one line saying what it is doing, so the terminal never looks frozen."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from yuyutsava.cli.entry import opens_a_chat
from yuyutsava.cli.startup_status import SLOW_AFTER_SECONDS, StartupStatus


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TheLine(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.stream = io.StringIO()
        self.line = StartupStatus(stream=self.stream, clock=self.clock)

    def tearDown(self) -> None:
        self.line.done()

    def drawn(self) -> str:
        return str(self.line._render().text)  # what the next refresh draws

    def test_says_the_step_in_words_and_clears_when_done(self) -> None:
        self.line.start("Starting YUYUTSAVA…")
        self.assertTrue(self.line.active)
        self.line.step("Opening your saved conversations…")
        self.assertEqual(self.drawn(), "Opening your saved conversations…")
        self.line.done()
        self.assertFalse(self.line.active)

    def test_a_long_step_says_it_is_still_working(self) -> None:
        self.line.start("Starting YUYUTSAVA…")
        self.line.step("Getting the agent ready…", slow_hint="the first start takes longest")
        self.assertNotIn("first start", self.drawn())
        self.clock.now += SLOW_AFTER_SECONDS + 1
        self.assertIn("the first start takes longest", self.drawn())

    def test_steps_aside_for_a_question_and_comes_back(self) -> None:
        self.line.start("Starting YUYUTSAVA…")
        with mock.patch.object(self.line._live, "stop") as stop:
            self.line.pause()
            stop.assert_called_once()
        with mock.patch.object(self.line._live, "start") as start:
            self.line.resume()
            start.assert_called_once()

    def test_names_each_steps_time_for_verbose_runs(self) -> None:
        self.line.start("Starting YUYUTSAVA…")
        self.clock.now += 1.25
        with self.assertLogs("yuyutsava.cli.startup_status", "DEBUG") as logs:
            self.line.step("Getting the agent ready…")
            self.clock.now += 0.5
            self.line.done()
        said = "\n".join(logs.output)
        self.assertIn("start-up: Starting YUYUTSAVA 1.2s", said)
        self.assertIn("start-up: Getting the agent ready 0.5s", said)
        self.assertIn("ready after 1.8s", said)


class OffATerminal(unittest.TestCase):
    def test_draws_nothing(self) -> None:
        line = StartupStatus(clock=_Clock())
        with mock.patch("os.isatty", return_value=False):
            line.start("Starting YUYUTSAVA…")
        self.assertFalse(line.active)
        line.done()


class WhichRunsShowIt(unittest.TestCase):
    def test_runs_that_open_a_chat(self) -> None:
        self.assertTrue(opens_a_chat([]))
        self.assertTrue(opens_a_chat(["chat"]))
        self.assertTrue(opens_a_chat(["chat", "--continue"]))

    def test_not_other_commands_or_listings(self) -> None:
        for argv in (["models", "list"], ["daemon"], ["--help"], ["chat", "--list-sessions"]):
            with self.subTest(argv=argv):
                self.assertFalse(opens_a_chat(argv))


if __name__ == "__main__":
    unittest.main()
