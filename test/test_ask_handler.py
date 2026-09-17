"""Approval-prompt hardening: a blank/leftover stdin line must NOT be read as a
rejection. Regression test for the bug where, under several parallel asks, a
stray empty line silently turned a `y`/`s` into a denied DELETE.

The handler is a closure built by ``make_ask_handler``, and it reads through
``cli.line_reader.read_line``, never ``input()`` — in the split view a
prompt_toolkit application owns stdin in raw mode, so a blocking ``input()``
anywhere else in the process never sees a keystroke. This file used to import
the closure by name and patch ``builtins.input``; neither existed, so it errored
on import and never ran (`R-TST-10`).
"""

from __future__ import annotations

import contextlib
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.cli.commands import chat_repl
from yuyutsava.cli.commands.chat_repl import make_ask_handler

_PAYLOAD = {
    "type": "task_runner_permission",
    "operation": "delete",
    "paths": ["/ws/Archives/sample.zip"],
    "zone": "workspace",
    "risk_level": "MEDIUM",
}


class _NoPauseRenderer:
    """The only thing the handler asks of a renderer is that it can pause."""

    @contextlib.contextmanager
    def pause(self):
        yield


def _lines(values):
    """Patch the module's ``read_line`` to yield *values*, then report EOF.

    ``read_line`` returns ``None`` when the line cannot be read (EOF, Ctrl-C, a
    broken front end), which is how the exhausted case is spelled here.
    """
    it = iter(values)

    async def _read(_prompt):
        return next(it, None)

    return mock.patch.object(chat_repl, "read_line", _read)


class AskHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ask = make_ask_handler(_NoPauseRenderer())

    async def test_blank_lines_skipped_then_session(self):
        with _lines(["", "   ", "s"]):
            result = await self.ask(_PAYLOAD)
        self.assertEqual(result, "approve_session")

    async def test_unrecognized_then_reject(self):
        with _lines(["maybe?", "n"]):
            result = await self.ask(_PAYLOAD)
        self.assertEqual(result, "reject")

    async def test_plain_yes(self):
        with _lines(["y"]):
            result = await self.ask(_PAYLOAD)
        self.assertEqual(result, "approve")

    async def test_all_blank_falls_back_to_reject(self):
        with _lines(["", "", ""]):
            result = await self.ask(_PAYLOAD)
        self.assertEqual(result, "reject")

    async def test_eof_rejects(self):
        # read_line reports an unreadable line as None, not by raising.
        with _lines([]):
            result = await self.ask(_PAYLOAD)
        self.assertEqual(result, "reject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
