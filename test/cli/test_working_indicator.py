"""The mark that says a turn is still alive moves, and stops when output does.

A turn spends most of its time waiting — on the first token, on a tool, on a
subagent — and a status line that does not move cannot be told apart from a
wedged process. That is exactly the moment a reader reaches for Ctrl+C.

The phase maths is shared: the scrolling transcript draws it into a Rich
``Live`` region and the split view into its status bar, so a second copy would
be a second thing to get out of step.

Run:  .venv/bin/python -m pytest test/cli/test_working_indicator.py
"""

from __future__ import annotations

import asyncio
import io
import re
import unittest
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from yuyutsava.ansi import palette
from yuyutsava.cli.render import pulse
from yuyutsava.cli.render.ansi_wrap import visible_width
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.dashboard import STYLE, ChatDashboard
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.renderer import RichChatRenderer

_ESC = re.compile(r"\x1b\[[0-9;]*m")


class TheMarkMoves(unittest.TestCase):
    def test_a_frame_advances_with_the_clock(self) -> None:
        first = pulse.frame_at(0.0)
        later = pulse.frame_at(pulse.FRAME_SEC)
        self.assertNotEqual(first, later, "the mark stood still between frames")

    def test_it_returns_to_the_start_rather_than_running_out(self) -> None:
        cycle = pulse.FRAME_SEC * len(pulse.FRAMES)
        self.assertEqual(pulse.frame_at(0.0), pulse.frame_at(cycle))
        self.assertEqual(pulse.frame_at(1000.0), pulse.frame_at(1000.0 + cycle))

    def test_the_colour_changes_too_and_walks_the_palette(self) -> None:
        seen = {pulse.hue_at(i * pulse.HUE_SEC) for i in range(len(pulse.HUES))}
        self.assertEqual(seen, set(pulse.HUES))

    def test_colour_and_frame_are_out_of_phase(self) -> None:
        """Two rhythms read as motion; one read as a blinking light."""
        self.assertNotEqual(pulse.FRAME_SEC, pulse.HUE_SEC)

    def test_every_hue_is_a_real_role(self) -> None:
        ramp = palette()
        for role in pulse.HUES:
            with self.subTest(role=role):
                self.assertIn(role, ramp)

    def test_every_frame_is_one_cell(self) -> None:
        """A two-cell frame would shift the label beside it as it animated."""
        for frame in pulse.FRAMES + pulse.RESTING:
            with self.subTest(frame=frame):
                self.assertEqual(visible_width(frame), 1)


class ItSettlesWhenOutputArrives(unittest.TestCase):
    def test_waiting_states_animate(self) -> None:
        for state in ("thinking", "tool", "subagent", "retry"):
            with self.subTest(state=state):
                self.assertTrue(pulse.is_animated(state))

    def test_writing_does_not(self) -> None:
        """Tokens landing on screen say the turn is alive better than a mark."""
        self.assertFalse(pulse.is_animated("writing"))
        self.assertEqual(pulse.mark_at(0.0, "writing"), pulse.RESTING)
        self.assertEqual(pulse.mark_at(99.0, "writing"), pulse.RESTING)

    def test_an_idle_renderer_animates_nothing(self) -> None:
        self.assertFalse(pulse.is_animated(""))


class BothSurfacesDrawIt(unittest.TestCase):
    def _rich(self) -> RichChatRenderer:
        return RichChatRenderer(
            verbose=False,
            workspace=Path("/tmp"),
            console=make_console(file=io.StringIO(), width=70, force_terminal=True),
            live=False,
        )

    def test_the_renderer_reports_its_phase_not_only_its_wording(self) -> None:
        renderer = self._rich()
        self.assertEqual(renderer.status_state(), "", "idle until a turn starts")
        renderer.begin_turn()
        self.assertEqual(renderer.status_state(), "thinking")

    def test_the_plain_renderer_has_no_phase(self) -> None:
        self.assertEqual(ChatRenderer(verbose=False, workspace=Path("/tmp")).status_state(), "")

    def test_the_live_region_redraws_from_the_clock(self) -> None:
        """Built once and handed to Live, so it must not be a fixed string."""
        renderer = self._rich()
        renderer.begin_turn()
        console = make_console(file=io.StringIO(), width=70, force_terminal=True)
        renderable = renderer._status_renderable()
        frames = set()
        for _ in range(4):
            with console.capture() as cap:
                console.print(renderable)
            frames.add(_ESC.sub("", cap.get()).strip()[:1])
            import time as _t

            _t.sleep(pulse.FRAME_SEC + 0.01)
        self.assertGreater(len(frames), 1, "the same renderable drew the same frame")

    def test_the_status_bar_has_a_style_for_every_hue(self) -> None:
        for role in pulse.HUES:
            with self.subTest(role=role):
                self.assertIn(f"pulse.{role}", STYLE)

    def test_the_status_bar_marks_a_wait_and_settles_on_output(self) -> None:
        async def _drive() -> tuple[list, list, list]:
            with create_pipe_input() as pipe:
                dash = ChatDashboard(history_path=None, output=DummyOutput(), input=pipe)
                dash._status = lambda: "Thinking…"
                dash._state = lambda: "thinking"
                first = dash._status_text()
                await asyncio.sleep(pulse.FRAME_SEC + 0.02)
                second = dash._status_text()
                dash._state = lambda: "writing"
                dash._status = lambda: "Writing…"
                return first, second, dash._status_text()

        first, second, settled = asyncio.run(_drive())
        self.assertNotEqual(first[0][1], second[0][1], "the bar's mark did not move")
        self.assertEqual(settled[0][1], f"{pulse.RESTING} ")

    def test_an_idle_status_bar_draws_no_mark(self) -> None:
        async def _idle() -> list:
            with create_pipe_input() as pipe:
                dash = ChatDashboard(history_path=None, output=DummyOutput(), input=pipe)
                return dash._status_text()

        self.assertEqual(asyncio.run(_idle()), [("class:status", "")])


if __name__ == "__main__":
    unittest.main()
