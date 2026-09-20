"""The subcommands: one list, and both the help and the dispatch read it.

`R-KIS-01`: the commands were named twice — a sentence in the parser
description ("Subcommands (not listed below): chat, daemon, attach, prefs") and
an if-chain in `main` — with nothing tying them together. They happened to
agree; `/help` and the tab-completer did not, and four working commands went
undocumented for it (`R-BUG-12`).

These pin the surface and the routing, so no command can be lost or silently
stop being advertised.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from yuyutsava.cli import cli

#: Every command a user can type. Pinned so removing one is a deliberate act.
EXPECTED = {"chat", "daemon", "attach", "prefs"}


class TheSubcommandSurface(unittest.TestCase):
    def test_the_registry_lists_every_command(self) -> None:
        self.assertEqual({c.name for c in cli.SUBCOMMANDS}, EXPECTED)

    def test_no_command_is_declared_twice(self) -> None:
        names = [c.name for c in cli.SUBCOMMANDS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_command_says_what_it_does(self) -> None:
        for cmd in cli.SUBCOMMANDS:
            with self.subTest(cmd=cmd.name):
                self.assertTrue(cmd.summary.strip())

    def test_help_lists_exactly_the_registry(self) -> None:
        """The invariant: the help is generated, so it cannot drift again."""
        help_text = cli._build_parser().format_help()
        for cmd in cli.SUBCOMMANDS:
            with self.subTest(cmd=cmd.name):
                self.assertIn(cmd.name, help_text)
                self.assertIn(cmd.summary, help_text)


class Routing(unittest.TestCase):
    """Driven against a synthetic registry, so no real command is invoked."""

    def _registry(self, seen: list[tuple[str, list[str]]]):
        def handler(name: str):
            def _run(argv: list[str]) -> int:
                seen.append((name, argv))
                return 0

            return _run

        return (
            cli.Subcommand("alpha", "the first", handler("alpha")),
            cli.Subcommand("beta", "the second", handler("beta"), quiet_interrupt=True),
        )

    def test_a_command_gets_the_argv_after_its_name(self) -> None:
        seen: list[tuple[str, list[str]]] = []
        rc = cli.dispatch(["alpha", "--flag", "value"], self._registry(seen))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [("alpha", ["--flag", "value"])])

    def test_an_unknown_first_word_is_a_task_not_an_error(self) -> None:
        """`yuyutsava "summarise this"` has to keep working."""
        seen: list[tuple[str, list[str]]] = []
        with mock.patch.object(cli, "_run_task", return_value=7) as task:
            rc = cli.dispatch(["summarise", "this"], self._registry(seen))
        self.assertEqual(rc, 7)
        task.assert_called_once_with(["summarise", "this"])
        self.assertEqual(seen, [], "no subcommand should have run")

    def test_no_arguments_is_a_task_too(self) -> None:
        """Bare `yuyutsava` drops into the REPL via the task path."""
        with mock.patch.object(cli, "_run_task", return_value=0) as task:
            cli.dispatch([], self._registry([]))
        task.assert_called_once_with([])

    def test_a_word_that_merely_starts_like_a_command_is_a_task(self) -> None:
        seen: list[tuple[str, list[str]]] = []
        with mock.patch.object(cli, "_run_task", return_value=0) as task:
            cli.dispatch(["chatter", "on"], self._registry(seen))
        task.assert_called_once()
        self.assertEqual(seen, [])


class CtrlCHandling(unittest.TestCase):
    def test_a_quiet_command_exits_130_without_a_traceback(self) -> None:
        def boom(argv: list[str]) -> int:
            raise KeyboardInterrupt

        registry = (cli.Subcommand("beta", "quiet", boom, quiet_interrupt=True),)
        self.assertEqual(cli.dispatch(["beta"], registry), 130)

    def test_a_command_that_handles_its_own_is_left_alone(self) -> None:
        """`attach` and `daemon` catch their own Ctrl+C; wrapping them twice
        would hide whatever they chose to do about it."""

        def boom(argv: list[str]) -> int:
            raise KeyboardInterrupt

        registry = (cli.Subcommand("alpha", "loud", boom),)
        with self.assertRaises(KeyboardInterrupt):
            cli.dispatch(["alpha"], registry)

    def test_the_task_path_is_quiet(self) -> None:
        with mock.patch.object(cli, "_run_task", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.dispatch(["some", "task"], ()), 130)


class TheDaemonStaysUnimported(unittest.TestCase):
    def test_importing_the_cli_does_not_load_the_daemon(self) -> None:
        """The daemon adds ~195 modules no other command needs, so its import
        lives inside the handler. A module-level one would cost every
        `--list-sessions` and every tab-completion the same ~120 ms."""
        self.assertIn("yuyutsava.cli.cli", sys.modules)
        self.assertNotIn(
            "yuyutsava.daemon.main",
            sys.modules,
            "the daemon was imported before its command ran",
        )


if __name__ == "__main__":
    unittest.main()
