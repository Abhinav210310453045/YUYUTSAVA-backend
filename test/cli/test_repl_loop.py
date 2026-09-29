"""`ReplLoop`: what the chat REPL does with each line it reads, and when it stops.

Commands go to the dispatcher, everything else is a turn, a finished turn
prints what it cost, and the loop ends on `/quit`, Ctrl+D or Ctrl+C at the
prompt. Background events are flushed before every read.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from yuyutsava.cli.commands import chat_repl as cr


class _Approvals:
    def __init__(self) -> None:
        self.flushes = 0

    async def render_between_turns(self) -> None:
        self.flushes += 1


class _Commands:
    """Dispatcher stand-in: `/quit` quits, `/x` is handled, the rest is a message."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.session = SimpleNamespace(thread_id="thr-1")

    async def dispatch(self, text: str) -> cr.Outcome:
        self.seen.append(text)
        if text == "/quit":
            return cr.Outcome.QUIT
        if text == "/retry":
            return cr.Outcome.RETRY
        if text.startswith("/"):
            if text == "/new":
                self.session = SimpleNamespace(thread_id="thr-2")
            return cr.Outcome.HANDLED
        return cr.Outcome.NOT_A_COMMAND


def _reader(*lines: Any):
    """Yields each line; an exception instance is raised instead."""
    it = iter(lines)

    async def _read() -> str:
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return item

    return _read


class _EchoingRenderer:
    """Records what the loop asked it to attribute to the user."""

    def __init__(self) -> None:
        self.echoed: list[str] = []

    def user_message(self, text: str, *, when: str = "") -> None:
        self.echoed.append(text)


def _run(*lines: Any, turn_ok: bool = True, usage: str = "1 call", echo: bool = False) -> dict:
    commands, approvals = _Commands(), _Approvals()
    renderer = _EchoingRenderer()
    turns: list[str] = []
    usage_calls: list[tuple] = []

    async def _one_turn(convo, text, **kw):
        turns.append("(retry)" if kw.get("retry") else text)
        return turn_ok

    async def _session_usage(store, thread_id, started):
        usage_calls.append((store, thread_id))
        return "ROWS"

    loop = cr.ReplLoop(
        commands=commands,
        convo="CONVO",
        renderer=renderer,
        ask_handler=None,
        dashboard=None,
        approvals=approvals,
        usage_store="USAGE",
        read_input=_reader(*lines),
        echo_input=echo,
    )
    err = io.StringIO()
    with (
        mock.patch.object(cr, "run_one_turn", _one_turn),
        mock.patch.object(cr, "session_usage", _session_usage),
        mock.patch.object(cr, "format_usage", lambda rows: usage),
        contextlib.redirect_stderr(err),
    ):
        asyncio.run(asyncio.wait_for(loop.run(), 10))
    return {
        "dispatched": commands.seen,
        "turns": turns,
        "usage_calls": usage_calls,
        "flushes": approvals.flushes,
        "err": err.getvalue(),
        "echoed": renderer.echoed,
    }


class WhatEachLineDoes(unittest.TestCase):
    def test_commands_messages_and_blank_lines(self) -> None:
        out = _run("  hello  ", "", "   ", "/x", "again", "/quit", "never read")
        self.assertEqual(out["dispatched"], ["hello", "/x", "again", "/quit"])
        self.assertEqual(out["turns"], ["hello", "again"])

    def test_retry_runs_a_turn_that_sends_nothing(self) -> None:
        out = _run("hi", "/retry", "/quit")
        self.assertEqual(out["turns"], ["hi", "(retry)"])
        self.assertEqual(len(out["usage_calls"]), 2, "a retry's cost is printed too")

    def test_a_turn_prints_its_cost_for_the_current_session(self) -> None:
        out = _run("hi", "/new", "hi", "/quit")
        self.assertEqual(out["usage_calls"], [("USAGE", "thr-1"), ("USAGE", "thr-2")])
        self.assertEqual(out["err"].count("1 call"), 2)

    def test_a_failed_turn_prints_no_cost(self) -> None:
        out = _run("hi", "/quit", turn_ok=False)
        self.assertEqual(out["turns"], ["hi"])
        self.assertEqual(out["usage_calls"], [])

    def test_an_empty_cost_prints_nothing(self) -> None:
        out = _run("hi", "/quit", usage="")
        self.assertEqual(out["err"], "")


class TheUsersOwnMessagesReachTheTranscript(unittest.TestCase):
    """Nothing printed them before: in the split view prompt_toolkit erases the
    line on submit, so the transcript read as an unattributed monologue."""

    def test_every_line_the_loop_acts_on_is_attributed(self) -> None:
        out = _run("hello", "/x", "again", "/quit", echo=True)
        self.assertEqual(out["echoed"], ["hello", "/x", "again", "/quit"])

    def test_blank_lines_are_not_attributed(self) -> None:
        out = _run("", "   ", "hi", "/quit", echo=True)
        self.assertEqual(out["echoed"], ["hi", "/quit"])

    def test_a_terminal_that_echoes_for_us_is_not_double_printed(self) -> None:
        """The negative control. At a rich prompt on a TTY the line is already
        on screen, and printing it again would show it twice."""
        out = _run("hello", "/quit", echo=False)
        self.assertEqual(out["echoed"], [])


class WhenItStops(unittest.TestCase):
    def test_ctrl_d_and_ctrl_c_end_cleanly(self) -> None:
        for exc in (EOFError(), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                out = _run("hi", exc, "never read")
                self.assertEqual(out["turns"], ["hi"])
                self.assertTrue(out["err"].endswith("\n"))

    def test_background_events_are_flushed_before_every_read(self) -> None:
        out = _run("a", "", "/x", "/quit")
        self.assertEqual(out["flushes"], 4)


if __name__ == "__main__":
    unittest.main()
