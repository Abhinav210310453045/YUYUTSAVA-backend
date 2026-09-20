"""The transcript sink and the redirection, driven without an application.

`R-SOL-01`: capturing the process's output was one of ten jobs inside
`ChatDashboard`, so the only way to exercise it was to build a full-screen
prompt_toolkit application first. It is process-global state with a strict
acquire/release shape and nothing to do with what the panes draw, which is why
it is now its own object — and why these tests need no application at all.

The dashboard's own tests still drive it through `dash.capture`; these pin the
behaviour that has to hold whoever installs it.
"""

from __future__ import annotations

import io
import logging
import sys
import unittest
import warnings

from yuyutsava.cli.render.stdio_capture import StdioCapture, TranscriptBuffer


class TheBufferHoldsLines(unittest.TestCase):
    def test_a_line_is_committed_only_on_a_newline(self) -> None:
        """Rich emits escape sequences and text as separate writes."""
        buf = TranscriptBuffer()
        buf.write("half ")
        self.assertEqual(buf.lines(), ["half "], "a partial line is still readable")
        buf.write("a line\n")
        self.assertEqual(buf.lines(), ["half a line"])

    def test_it_refuses_to_hand_out_a_descriptor(self) -> None:
        """A subprocess inheriting fd 1 would write straight past the pane, so
        reaching for one must fail loudly rather than quietly succeed."""
        with self.assertRaises(io.UnsupportedOperation):
            TranscriptBuffer().fileno()

    def test_it_claims_to_be_a_terminal(self) -> None:
        """Rich asks, and the destination really is one — one pane over."""
        self.assertTrue(TranscriptBuffer().isatty())


class TheCaptureRestoresWhatItTook(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = TranscriptBuffer()
        self.capture = StdioCapture(self.buffer)
        self.saved = (sys.stdout, sys.stderr)
        self.addCleanup(self.capture.restore)

    def test_stdout_and_stderr_round_trip(self) -> None:
        self.capture.install()
        self.assertIs(sys.stdout, self.buffer)
        print("into the pane")
        self.capture.restore()
        self.assertEqual((sys.stdout, sys.stderr), self.saved)
        self.assertIn("into the pane", "\n".join(self.buffer.lines()))

    def test_restoring_without_installing_is_harmless(self) -> None:
        """Teardown runs even when start-up failed part-way."""
        self.capture.restore()
        self.assertEqual((sys.stdout, sys.stderr), self.saved)

    def test_restoring_twice_is_harmless(self) -> None:
        self.capture.install()
        self.capture.restore()
        self.capture.restore()
        self.assertEqual((sys.stdout, sys.stderr), self.saved)

    def test_a_handler_bound_to_the_old_stderr_is_retargeted(self) -> None:
        """A StreamHandler keeps the stream it was given, so replacing
        sys.stderr does not move it — it would write through the application."""
        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stderr)
        root.addHandler(handler)
        try:
            original = handler.stream
            self.capture.install()
            self.assertIs(handler.stream, self.buffer)
            logging.getLogger("test.stdio").error("logged while captured")
            self.capture.restore()
            self.assertIs(handler.stream, original)
        finally:
            root.removeHandler(handler)
        self.assertIn("logged while captured", "\n".join(self.buffer.lines()))

    def test_a_handler_below_a_non_propagating_logger_is_found(self) -> None:
        """Walking only the root logger is not enough: setup_logging attaches
        the CLI's handler to `yuyutsava` with propagate=False."""
        quiet = logging.getLogger("yuyutsava.test.nonpropagating")
        quiet.propagate = False
        handler = logging.StreamHandler(sys.stderr)
        quiet.addHandler(handler)
        try:
            self.capture.install()
            self.assertIs(handler.stream, self.buffer, "a nested handler must be retargeted")
            self.capture.restore()
        finally:
            quiet.removeHandler(handler)

    def test_a_warning_lands_in_the_pane(self) -> None:
        """warnings.showwarning holds the stderr it captured earlier; Google's
        credentials warning arrives that way and would be the first line."""
        self.capture.install()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("always")
                warnings.warn("a warning during the app", UserWarning, stacklevel=1)
        finally:
            self.capture.restore()
        self.assertIn("a warning during the app", "\n".join(self.buffer.lines()))

    def test_the_transcript_can_be_handed_back(self) -> None:
        """The alternate screen takes the session's output with it on exit."""
        self.buffer.write("first\nsecond\n")
        sink = io.StringIO()
        self.capture.replay_to(sink)
        self.assertEqual(sink.getvalue(), "first\nsecond\n")


if __name__ == "__main__":
    unittest.main()
