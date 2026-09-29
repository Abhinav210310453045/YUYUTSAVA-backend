"""Plumbing does not write into the chat transcript at column zero.

Two things reached the pane without passing through a renderer, and landed
between the user's message and the first tool line: an INFO record naming the
skills retrieval had matched, and the stdlib's two-line warning format with an
absolute site-packages path in it. Both are still reported — one through the
renderer, one as a themed row — and neither is suppressed.

The banner's own colours are pinned here too: its rows name facts about the
session, not anything in the conversation, so they must not be drawn in a hue
the transcript uses.

Run:  .venv/bin/python -m pytest test/cli/test_transcript_is_only_transcript.py
"""

from __future__ import annotations

import io
import re
import unittest
import warnings
from pathlib import Path

from yuyutsava import ansi
from yuyutsava.cli.render import glyphs as gl
from yuyutsava.cli.render import startup
from yuyutsava.cli.render.ansi_wrap import HANG, wrap_ansi
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.notices import warning_rows
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.cli.render.stdio_capture import StdioCapture, TranscriptBuffer
from yuyutsava.cli.render.tool_format import fit_names

_ESC = re.compile(r"\x1b\[[0-9;]*m")

_NAMES = (
    "tool-failure-recovery",
    "todo-capture-playbook",
    "macos-floating-vitals-widget",
    "bg-task-status-verification",
    "geopolitical-report-compilation",
)

_ADC = (
    "Your application has authenticated using end user credentials from Google "
    "Cloud SDK without a quota project. You might receive a quota exceeded error."
)
_SITE_PACKAGES = (
    "/Users/x/.local/share/uv/tools/yuyutsava/lib/python3.12/site-packages/google/auth/_default.py"
)


def _plain(text: str) -> str:
    return _ESC.sub("", text)


class TheBannerUsesItsOwnColours(unittest.TestCase):
    """Its rows describe the session, not the conversation. Drawing them in a
    transcript hue invites reading a session id as if it were a tool result."""

    _TRANSCRIPT_ROLES = ("you", "agent", "tool", "ok", "err", "subagent", "chrome")

    def test_the_key_and_value_hues_are_not_transcript_hues(self) -> None:
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                ramp = ansi.palette(theme)
                used = {ramp[role] for role in self._TRANSCRIPT_ROLES}
                self.assertNotIn(ramp["key"], used)
                self.assertNotIn(ramp["value"], used)
                self.assertNotEqual(ramp["key"], ramp["value"])

    def test_both_ramps_carry_them(self) -> None:
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                self.assertIn("key", ansi.palette(theme))
                self.assertIn("value", ansi.palette(theme))

    def test_a_key_is_bold_and_coloured(self) -> None:
        line = startup._row("workspace", "/tmp/ws", 9)
        self.assertIn(ansi.BOLD, line)
        self.assertIn(ansi.KEY, line)
        self.assertIn(ansi.VALUE, line)

    def test_every_fact_is_a_labelled_row(self) -> None:
        err = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(err):
            startup.print_banner(
                startup.BannerFacts(
                    session_id="cli-1",
                    workspace=Path("/tmp/ws"),
                    status="postgres · tracing off",
                    notice="langgraph-api 0.15.1 available",
                ),
                width=118,
            )
        text = _plain(err.getvalue())
        for label in ("workspace", "session", "runtime", "warning", "help"):
            with self.subTest(label=label):
                # Compiled rather than passed as a flag: assertRegex's third
                # argument is the failure message, so a flag handed to it is
                # silently a message and the pattern loses MULTILINE.
                self.assertRegex(text, re.compile(rf"^\s+{label}\s+\S", re.MULTILINE))

    def test_the_warning_row_keeps_the_warning_colour(self) -> None:
        """Presented as a row like the others, but a warning that looks exactly
        like a fact is not doing its job."""
        err = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(err):
            startup.print_banner(
                startup.BannerFacts(
                    session_id="cli-1",
                    workspace=Path("/tmp/ws"),
                    notice="an update is available",
                ),
                width=118,
            )
        row = next(ln for ln in err.getvalue().splitlines() if "warning" in _plain(ln))
        self.assertIn(ansi.WARN, row)
        self.assertNotIn(ansi.KEY, row)


class AWarningSitsOnTheLadder(unittest.TestCase):
    """Two lines: what warned, then what it said at the body indent.

    On one line the message is longer than the pane and its continuation lands
    under the label, reading as a second warning. Given a line of its own the
    message wraps under itself, on the same ladder as every other block.
    """

    def _rows(self, message: object = _ADC) -> list[str]:
        return [_plain(r) for r in warning_rows(message, UserWarning, _SITE_PACKAGES, 114)]

    def test_the_label_names_the_category_and_is_punctuated(self) -> None:
        self.assertIn("⚠ UserWarning:", self._rows()[0])

    def test_the_absolute_path_is_reduced_to_the_module(self) -> None:
        head = self._rows()[0]
        self.assertNotIn("site-packages", head)
        self.assertIn("_default:114", head)

    def test_the_message_is_a_line_of_its_own_at_the_body_indent(self) -> None:
        head, body = self._rows()
        self.assertNotIn("without a quota project", head)
        self.assertIn("without a quota project", body, "nothing is suppressed")
        self.assertTrue(body.startswith("    "))

    def test_the_label_is_at_the_marker_indent(self) -> None:
        self.assertTrue(self._rows()[0].startswith("  ⚠"), "column zero is the defect")

    def test_a_multi_line_message_is_flattened_onto_the_one_row(self) -> None:
        rows = self._rows("first\n  second\n\nthird")
        self.assertEqual(len(rows), 2)
        self.assertIn("first second third", rows[1])

    def test_the_message_wraps_under_itself(self) -> None:
        """The whole point: every row of it starts in the same column."""
        _head, body = warning_rows(_ADC, UserWarning, _SITE_PACKAGES, 114)
        rows = wrap_ansi(body, 70, hang=HANG)
        self.assertGreater(len(rows), 1)
        starts = {len(_plain(r)) - len(_plain(r).lstrip()) for r in rows}
        self.assertEqual(starts, {4})

    def test_the_capture_writes_those_rows_and_no_others(self) -> None:
        buffer = TranscriptBuffer()
        capture = StdioCapture(buffer)
        try:
            capture.install()
            warnings.warn(_ADC, UserWarning, stacklevel=1)
        finally:
            capture.restore()
        rows = [ln for ln in buffer.lines() if ln.strip()]
        self.assertEqual(len(rows), 2, f"expected two rows, got {rows}")
        self.assertIn("⚠", _plain(rows[0]))
        self.assertNotIn("site-packages", _plain(rows[0]))
        self.assertNotIn("warnings.warn(", "\n".join(_plain(r) for r in rows))


class RecalledSkillsAreDrawnByTheRenderer(unittest.TestCase):
    def _rich(self, width: int = 88) -> tuple[RichChatRenderer, io.StringIO]:
        buf = io.StringIO()
        return (
            RichChatRenderer(
                verbose=False,
                workspace=Path("/tmp"),
                console=make_console(file=buf, width=width, force_terminal=False),
                live=False,
            ),
            buf,
        )

    def test_the_rich_renderer_draws_one_marked_row(self) -> None:
        renderer, buf = self._rich()
        renderer.skills_recalled(_NAMES)
        rows = [ln for ln in _plain(buf.getvalue()).splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith(f"  {gl.SKILLS} skills"))
        self.assertIn("tool-failure-recovery", rows[0])

    def test_the_plain_renderer_draws_one_too(self) -> None:
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ChatRenderer(verbose=False, workspace=Path("/tmp")).skills_recalled(_NAMES)
        rows = [ln for ln in _plain(err.getvalue()).splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn("skills", rows[0])

    def test_no_skills_draws_nothing(self) -> None:
        renderer, buf = self._rich()
        renderer.skills_recalled(())
        self.assertEqual(buf.getvalue(), "")

    def test_the_row_never_paints_past_the_pane(self) -> None:
        for width in (40, 60, 88, 120):
            with self.subTest(width=width):
                renderer, buf = self._rich(width)
                renderer.skills_recalled(_NAMES)
                for line in _plain(buf.getvalue()).splitlines():
                    self.assertLessEqual(len(line), width)

    def test_what_did_not_fit_is_counted_not_dropped(self) -> None:
        """A row showing three of five names claims the turn saw three."""
        shown, rest = fit_names(_NAMES, 60)
        self.assertGreater(rest, 0)
        self.assertEqual(rest + shown.count("·") + 1, len(_NAMES))

    def test_one_name_always_fits_however_narrow(self) -> None:
        shown, rest = fit_names(_NAMES, 4)
        self.assertEqual(shown, _NAMES[0])
        self.assertEqual(rest, len(_NAMES) - 1)


if __name__ == "__main__":
    unittest.main()
