"""Prose arrives smoothly, and scrolling does not recompute the world.

Two complaints, one about each end of the display:

* an answer appeared a whole paragraph at a time, because the transcript only
  commits a markdown block once that block is complete — so between blank
  lines nothing was on screen at all;
* the wheel stuttered, because working out how far it was allowed to scroll
  re-wrapped the entire scrollback on every tick.

Run:  .venv/bin/python -m pytest test/cli/test_prose_streaming.py
"""

from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

from yuyutsava.cli.render import prose_frame as pf
from yuyutsava.cli.render.ansi_wrap import HANG, char_width, row_count, wrap_ansi
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.cli.render.scroll_view import ScrollView
from yuyutsava.cli.render.stdio_capture import TranscriptBuffer
from yuyutsava.cli.stream_smoother import TokenSmoother, smoothing_enabled
from yuyutsava.core.streaming import StreamEvent

_ESC = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ESC.sub("", text)


def _renderer(buffer: TranscriptBuffer, width: int = 70) -> RichChatRenderer:
    renderer = RichChatRenderer(
        verbose=False,
        workspace=Path("/tmp"),
        console=make_console(file=buffer, width=width, force_terminal=True),
        live=False,
    )
    renderer.show_pending = buffer.set_tail
    return renderer


class AnAnswerAppearsWhileItIsWritten(unittest.IsolatedAsyncioTestCase):
    async def _feed(self, renderer, *chunks):
        for chunk in chunks:
            await renderer.render(StreamEvent(kind="token", data={"text": chunk}))

    async def test_text_is_on_screen_before_the_block_is_complete(self) -> None:
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        await self._feed(renderer, "The split ", "is computed ", "in one place")
        text = _plain("\n".join(buffer.lines()))
        self.assertIn("The split is computed in one place", text)
        self.assertIn(pf.TOP_LEFT, text, "and it is already inside the frame")
        self.assertNotIn(pf.BOTTOM_LEFT, text, "which is not closed yet")

    async def test_it_grows_rather_than_being_replaced_wholesale(self) -> None:
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        seen = []
        for chunk in ("one ", "two ", "three "):
            await self._feed(renderer, chunk)
            seen.append(_plain("\n".join(buffer.lines())))
        self.assertIn("one", seen[0])
        self.assertNotIn("two", seen[0])
        self.assertIn("two", seen[1])
        self.assertIn("three", seen[2])

    async def test_the_finished_block_replaces_the_provisional_rows(self) -> None:
        """The failure this guards is the paragraph appearing twice."""
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        await self._feed(renderer, "a whole paragraph", "\n\n")
        await renderer.end_of_turn()
        text = _plain("\n".join(buffer.lines()))
        self.assertEqual(text.count("a whole paragraph"), 1)
        self.assertIn(pf.BOTTOM_LEFT, text)

    async def test_the_provisional_rows_never_enter_the_scrollback(self) -> None:
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        await self._feed(renderer, "still being written")
        self.assertIn("still being written", _plain("\n".join(buffer.lines())))
        buffer.set_tail([])
        self.assertNotIn("still being written", _plain("\n".join(buffer.lines())))

    async def test_a_tool_call_takes_the_partial_text_off_the_screen(self) -> None:
        """It is committed properly first — nothing the model wrote is lost."""
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        await self._feed(renderer, "before the call")
        await renderer.render(StreamEvent(kind="tool_call", data={"name": "tr_ls", "args": {}}))
        lines = _plain("\n".join(buffer.lines())).splitlines()
        self.assertEqual(sum("before the call" in ln for ln in lines), 1)
        closed = next(i for i, ln in enumerate(lines) if pf.BOTTOM_LEFT in ln)
        called = next(i for i, ln in enumerate(lines) if "tr_ls" in ln)
        self.assertLess(closed, called)

    async def test_a_renderer_with_no_host_to_redraw_keeps_it_to_itself(self) -> None:
        """The negative control: without a sink there are no provisional rows,
        and the rich transcript behaves exactly as it did."""
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        renderer.show_pending = None
        await self._feed(renderer, "not shown yet")
        self.assertNotIn("not shown yet", _plain("\n".join(buffer.lines())))


class ProseIsPaced(unittest.IsolatedAsyncioTestCase):
    async def test_a_burst_is_released_a_few_characters_at_a_time(self) -> None:
        out: list[str] = []
        smoother = TokenSmoother(out.append, base_cps=400.0)
        smoother.feed("x" * 200)
        await asyncio.sleep(0.05)
        self.assertGreater(len(out), 1, "a burst arrived as one write")
        self.assertLess(len("".join(out)), 200, "and all of it was released at once")
        await smoother.aclose()
        self.assertEqual(len("".join(out)), 200, "nothing is dropped on the way")

    async def test_flush_now_empties_the_backlog_synchronously(self) -> None:
        """Ordering: a tool line must not print above prose that preceded it,
        and a synchronous printer cannot await a background task."""
        out: list[str] = []
        smoother = TokenSmoother(out.append, base_cps=1.0)
        smoother.feed("everything buffered")
        smoother.flush_now()
        self.assertEqual("".join(out), "everything buffered")
        await smoother.aclose()

    async def test_prose_lands_before_the_tool_line_that_follows_it(self) -> None:
        buffer = TranscriptBuffer()
        renderer = _renderer(buffer)
        renderer._smoother = TokenSmoother(renderer._feed_prose, base_cps=1.0)
        await renderer.render(StreamEvent(kind="token", data={"text": "said first"}))
        await renderer.render(StreamEvent(kind="tool_call", data={"name": "tr_ls", "args": {}}))
        lines = _plain("\n".join(buffer.lines())).splitlines()
        said = next(i for i, ln in enumerate(lines) if "said first" in ln)
        called = next(i for i, ln in enumerate(lines) if "tr_ls" in ln)
        self.assertLess(said, called)
        await renderer.aclose()

    def test_piped_output_is_not_paced(self) -> None:
        """Nobody is watching it appear, and pacing would only make the render
        path differ from printing the chunks straight through."""
        self.assertFalse(smoothing_enabled(), "stdout is not a tty under pytest")


class ScrollingDoesNotRewrapTheScrollback(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = [f"line {i} " + "word " * (i % 40) for i in range(2000)]
        self.version = [0]
        self.view = ScrollView(
            lines=lambda: self.lines,
            width=lambda: 80,
            height=lambda: 40,
            version=lambda: self.version[0],
        )

    @staticmethod
    def _lookups() -> int:
        """Every row-count access, cached or not — the walk itself is the cost.

        Counting misses alone would pass even with no memo at all, because the
        per-line cache absorbs the recomputation: the walk still visits all
        2,000 lines, it just finds each answer already there. What has to stop
        happening is the walk.
        """
        info = row_count.cache_info()
        return info.hits + info.misses

    def test_the_total_is_computed_once_per_change_not_once_per_tick(self) -> None:
        self.view.display_count(80)
        before = self._lookups()
        for _ in range(50):
            self.view.scroll_by(3)
        self.assertEqual(
            self._lookups(),
            before,
            "a wheel tick walked the whole scrollback again",
        )

    def test_the_first_count_does_walk_it(self) -> None:
        """The positive half: the memo must not be answering from nothing."""
        before = self._lookups()
        self.view.display_count(80)
        self.assertGreaterEqual(self._lookups() - before, len(self.lines))

    def test_new_output_is_counted(self) -> None:
        first = self.view.display_count(80)
        self.lines.append("one more line")
        self.version[0] += 1
        self.assertGreater(self.view.display_count(80), first)

    def test_a_width_change_is_counted(self) -> None:
        wide = self.view.display_count(200)
        narrow = self.view.display_count(40)
        self.assertGreater(narrow, wide, "narrower means more wrapped rows")

    def test_without_a_version_it_still_answers_correctly(self) -> None:
        """The memo is an optimisation, not the behaviour."""
        plain = ScrollView(lines=lambda: self.lines, width=lambda: 80, height=lambda: 40)
        self.assertEqual(plain.display_count(80), self.view.display_count(80))

    def test_the_count_matches_what_wrapping_actually_produces(self) -> None:
        sample = self.lines[:200]
        view = ScrollView(lines=lambda: sample, width=lambda: 37, height=lambda: 10)
        self.assertEqual(
            view.display_count(37),
            sum(len(wrap_ansi(ln, 37, hang=HANG)) for ln in sample),
            "the count and the rows must agree about the continuation indent",
        )

    def test_row_count_agrees_with_wrap_ansi_on_awkward_input(self) -> None:
        for line in ("", "x", "\x1b[31mred\x1b[0m", "ま" * 30, "a" * 200, "\x1b[1m" * 40):
            for width in (1, 2, 5, 37, 200):
                with self.subTest(line=line[:12], width=width):
                    self.assertEqual(row_count(line, width), len(wrap_ansi(line, width)))


class AWrappedLineContinuesUnderItself(unittest.TestCase):
    """A second row flush against column zero reads as a new statement.

    Rich wraps its own output to the pane, so the lines that reach the wrapper
    are the ones nothing formatted — a ``print``, a log record, a warning. A
    long warning's tail therefore began a fresh line at column zero, under the
    mark rather than under the message.
    """

    def test_every_row_after_the_first_is_indented(self) -> None:
        rows = wrap_ansi("word " * 40, 60, hang=HANG)
        self.assertGreater(len(rows), 1)
        for row in rows[1:]:
            self.assertTrue(row.startswith(" " * HANG))

    def test_no_row_overflows_because_of_the_indent(self) -> None:
        from yuyutsava.cli.render.ansi_wrap import visible_width

        for width in (20, 37, 60, 90):
            with self.subTest(width=width):
                for row in wrap_ansi("word " * 40, width, hang=HANG):
                    self.assertLessEqual(visible_width(row), width)

    def test_a_narrow_pane_keeps_its_width_instead(self) -> None:
        """Those cells are the difference between a sentence and a stack of
        fragments, and the reader needs the text more than the alignment."""
        rows = wrap_ansi("z" * 40, 10, hang=HANG)
        self.assertFalse(rows[1].startswith(" "))

    def test_colour_still_survives_the_break(self) -> None:
        rows = wrap_ansi("\x1b[31m" + ("red " * 40), 60, hang=HANG)
        self.assertIn("\x1b[31m", rows[1], "the indent must not eat the reopened colour")


class TheBufferSaysWhenItChanged(unittest.TestCase):
    def test_a_write_bumps_the_version(self) -> None:
        buffer = TranscriptBuffer()
        before = buffer.version()
        buffer.write("a line\n")
        self.assertGreater(buffer.version(), before)

    def test_replacing_the_tail_with_the_same_rows_does_not(self) -> None:
        """Otherwise every unchanged repaint invalidates the memoised count."""
        buffer = TranscriptBuffer()
        buffer.set_tail(["row"])
        settled = buffer.version()
        buffer.set_tail(["row"])
        self.assertEqual(buffer.version(), settled)
        buffer.set_tail(["row", "another"])
        self.assertGreater(buffer.version(), settled)

    def test_clearing_drops_the_tail_too(self) -> None:
        buffer = TranscriptBuffer()
        buffer.write("committed\n")
        buffer.set_tail(["provisional"])
        buffer.clear()
        self.assertEqual(buffer.lines(), [])

    def test_char_width_is_cached_but_still_right(self) -> None:
        for ch, cells in (("a", 1), ("ま", 2), ("́", 0)):
            with self.subTest(ch=ch):
                self.assertEqual(char_width(ch), cells)
                self.assertEqual(char_width(ch), cells)


if __name__ == "__main__":
    unittest.main()
