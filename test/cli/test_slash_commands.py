"""The `/command` surface: one list, and everything reads from it.

`R-SOL-02`: the commands were declared in two hand-kept places — the completer's
dict and the body of `_print_help` — and dispatched in five more. They drifted
exactly as you would expect: `/asks`, `/approve`, `/reject` and `/reply` were
offered by tab-completion and missing from `/help`, so the only way to discover
how to answer a pending background ask was to press Tab and guess.

These pin the surface, and the invariant that stops it happening again.
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest

from yuyutsava.cli.commands import chat_repl as cr
from yuyutsava.cli.render import reports

#: The commands a user can type. Pinned so removing one is a deliberate act.
EXPECTED = {
    "/help",
    "/quit",
    "/clear",
    "/new",
    "/retry",
    "/session",
    "/ring",
    "/last",
    "/expand",
    "/asks",
    "/approve",
    "/reject",
    "/reply",
    "/voice",
    "/subagents",
    "/usage",
    "/context",
    "/skills",
}


def _help_text() -> str:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        reports.print_help(cr.SLASH_COMMANDS)
    return buf.getvalue()


def _commands_in_help() -> set[str]:
    """Commands `/help` lists — the first token of a line, not a word inside
    a description (`/ring`'s text mentions "tool calls/results")."""
    found = set()
    for line in _help_text().splitlines():
        m = re.match(r"\s*(?:\x1b\[[\d;]*m)?\s*(/[a-z]+)", line)
        if m:
            found.add(m.group(1))
    return found


class TheCommandSurface(unittest.TestCase):
    def test_the_registry_lists_every_command(self) -> None:
        self.assertEqual({c.name for c in cr.SLASH_COMMANDS}, EXPECTED)

    def test_no_command_is_declared_twice(self) -> None:
        names = [c.name for c in cr.SLASH_COMMANDS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_command_says_what_it_does(self) -> None:
        for cmd in cr.SLASH_COMMANDS:
            with self.subTest(cmd=cmd.name):
                self.assertTrue(cmd.help.strip())


class HelpAndCompletionAgree(unittest.TestCase):
    """The invariant. Both are generated, so they cannot drift apart again."""

    def test_help_lists_exactly_what_completion_offers(self) -> None:
        self.assertEqual(_commands_in_help(), set(cr._SLASH_COMMANDS))

    def test_the_background_approval_commands_are_documented(self) -> None:
        """The regression that motivated the registry.

        These four answer a pending ask from the daemon. Undocumented, a user
        with a blocked background task has no way to find them.
        """
        listed = _commands_in_help()
        for name in ("/asks", "/approve", "/reject", "/reply"):
            with self.subTest(cmd=name):
                self.assertIn(name, listed)

    def test_usage_hints_reach_the_help(self) -> None:
        """`/reply` without its arguments prints a usage line and does nothing,
        so the help has to show the shape."""
        self.assertIn("/reply <id> <text>", _help_text())
        self.assertIn("/last [k]", _help_text())


class EveryDocumentedCommandIsHandled(unittest.TestCase):
    def test_no_command_is_advertised_without_a_handler(self) -> None:
        """A command in the list that nothing dispatches answers "unknown
        command" — worse than not offering it, because the user believes it."""
        import inspect

        dispatchers = "".join(
            inspect.getsource(f)
            for f in (
                cr.ReplCommands,
                cr._handle_slash,
                cr._handle_settings_command,
                cr._handle_ask_command,
            )
        )
        for cmd in cr.SLASH_COMMANDS:
            with self.subTest(cmd=cmd.name):
                # assertTrue, not assertIn: assertIn prints both operands, and
                # one of them is every dispatcher in the module.
                self.assertTrue(
                    f'"{cmd.name}"' in dispatchers,
                    f"{cmd.name} is advertised but no dispatcher mentions it",
                )

    def test_the_quit_aliases_are_handled(self) -> None:
        src = __import__("inspect").getsource(cr._handle_slash)
        quit_cmd = next(c for c in cr.SLASH_COMMANDS if c.name == "/quit")
        for alias in quit_cmd.aliases:
            with self.subTest(alias=alias):
                self.assertTrue(f'"{alias}"' in src, f"{alias} is never dispatched")


if __name__ == "__main__":
    unittest.main()
