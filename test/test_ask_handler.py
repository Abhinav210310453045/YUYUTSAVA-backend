"""Approval-prompt hardening: a blank/leftover stdin line must NOT be read as a
rejection. Regression test for the bug where, under several parallel asks, a
stray empty line silently turned a `y`/`s` into a denied DELETE.
"""

from __future__ import annotations

import builtins
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.cli.commands.chat_repl import _ask_handler

_PAYLOAD = {
    "type": "task_runner_permission",
    "operation": "delete",
    "paths": ["/ws/Archives/sample.zip"],
    "zone": "workspace",
    "risk_level": "MEDIUM",
}


def _inputs(values):
    """Patch builtins.input to yield *values* in order (raising on exhaustion)."""
    it = iter(values)
    return mock.patch.object(builtins, "input", lambda *_a, **_k: next(it))


class AskHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_lines_skipped_then_session(self):
        with _inputs(["", "   ", "s"]):
            result = await _ask_handler(_PAYLOAD)
        self.assertEqual(result, "approve_session")

    async def test_unrecognized_then_reject(self):
        with _inputs(["maybe?", "n"]):
            result = await _ask_handler(_PAYLOAD)
        self.assertEqual(result, "reject")

    async def test_plain_yes(self):
        with _inputs(["y"]):
            result = await _ask_handler(_PAYLOAD)
        self.assertEqual(result, "approve")

    async def test_all_blank_falls_back_to_reject(self):
        with _inputs(["", "", ""]):
            result = await _ask_handler(_PAYLOAD)
        self.assertEqual(result, "reject")

    async def test_eof_rejects(self):
        def _raise(*_a, **_k):
            raise EOFError

        with mock.patch.object(builtins, "input", _raise):
            result = await _ask_handler(_PAYLOAD)
        self.assertEqual(result, "reject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
