"""``/clear`` in the dashboard clears the transcript pane instead of writing an
escape sequence into it (R-BUG-05).

In dashboard mode ``sys.stderr`` is the transcript buffer, so ``/clear``'s raw
``ESC[2J ESC[H`` landed in the pane as a line of text, and the transcript was
never cleared — only Ctrl+L did that.
"""

from __future__ import annotations

import contextlib
import unittest
from pathlib import Path

from yuyutsava.cli.commands import chat_repl
from yuyutsava.cli.render.stdio_capture import TranscriptBuffer


class _Dashboard:
    def __init__(self, buffer: TranscriptBuffer) -> None:
        self.buffer = buffer
        self.cleared = 0

    def clear_transcript(self) -> None:
        self.cleared += 1
        self.buffer.clear()


def _run_clear(**extra) -> TranscriptBuffer:
    buffer = TranscriptBuffer()
    buffer.write("an earlier turn\n")
    with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):  # type: ignore[type-var]
        chat_repl._handle_slash(
            chat_repl.CommandLine.parse("/clear"),
            session_id="s1",
            workspace=Path("/tmp"),
            renderer=None,
            **extra,
        )
    return buffer


class ClearInTheDashboard(unittest.TestCase):
    def test_no_escape_sequence_lands_in_the_pane(self) -> None:
        buffer = TranscriptBuffer()
        dashboard = _Dashboard(buffer)
        buffer = _run_clear(clear_screen=dashboard.clear_transcript)
        self.assertFalse(any("\x1b[2J" in line for line in buffer.lines()))

    def test_the_earlier_transcript_is_gone_and_the_banner_redrawn(self) -> None:
        buffer = TranscriptBuffer()
        buffer.write("an earlier turn\n")
        dashboard = _Dashboard(buffer)
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):  # type: ignore[type-var]
            chat_repl._handle_slash(
                chat_repl.CommandLine.parse("/clear"),
                session_id="s1",
                workspace=Path("/tmp"),
                renderer=None,
                clear_screen=dashboard.clear_transcript,
            )
        text = "\n".join(buffer.lines())
        self.assertEqual(dashboard.cleared, 1)
        self.assertNotIn("an earlier turn", text)
        self.assertIn("s1", text)


if __name__ == "__main__":
    unittest.main()
