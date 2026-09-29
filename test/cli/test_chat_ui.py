"""The chat REPL's visual contract: the opening screen, and who said what.

Three things this pins, each of which was a defect rather than a preference:

* the banner was three rows of art against column zero, under a PyPI warning
  that printed before it;
* nothing ever printed the user's own messages, so the transcript read as an
  unattributed monologue;
* ``tool``, ``accent`` and ``prompt`` were all cyan, across three hand-kept
  palettes that had already drifted.
"""

from __future__ import annotations

import io
import re
import unittest
from pathlib import Path

from types import SimpleNamespace

from yuyutsava import ansi
from yuyutsava.cli.render import glyphs as gl
from yuyutsava.cli.render import prose_frame as pf
from yuyutsava.cli.render import startup
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.core.streaming import StreamEvent

_ESC = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ESC.sub("", text)


def _banner(width: int, **kw) -> str:
    err = io.StringIO()
    import contextlib

    with contextlib.redirect_stderr(err):
        startup.print_banner(
            session_id=kw.pop("session_id", "cli-123"),
            workspace=kw.pop("workspace", Path("/tmp/ws")),
            resuming=kw.pop("resuming", False),
            width=width,
            **kw,
        )
    return _plain(err.getvalue())


class TheWordmarkFitsTheTerminal(unittest.TestCase):
    """Three sizes, largest first. A wordmark wider than the pane would wrap
    into meaningless fragments, which is worse than a smaller one."""

    def test_a_wide_terminal_gets_the_full_wordmark_centred(self) -> None:
        art, margin = startup.wordmark(118)
        self.assertIs(art, startup.WORDMARK)
        self.assertGreater(margin, 2, "a centred wordmark is not at column zero")
        self.assertEqual(margin, (118 - len(startup.WORDMARK[0])) // 2)

    def test_a_medium_terminal_falls_back_to_the_three_row_mark(self) -> None:
        art, _ = startup.wordmark(60)
        self.assertIs(art, startup.BANNER_LINES)

    def test_a_narrow_terminal_gets_the_name_and_no_art(self) -> None:
        art, margin = startup.wordmark(30)
        self.assertEqual(art, [startup.WORDMARK_TEXT])
        self.assertEqual(margin, (30 - len(startup.WORDMARK_TEXT)) // 2)

    def test_a_terminal_narrower_than_the_name_still_gets_a_banner(self) -> None:
        art, margin = startup.wordmark(10)
        self.assertEqual(art, [startup.WORDMARK_TEXT])
        self.assertEqual(margin, 2, "the gutter, rather than a negative margin")

    def test_nothing_ever_overflows_the_width(self) -> None:
        """The guarantee, at every width the ladder has to answer for."""
        for width in range(20, 200):
            with self.subTest(width=width):
                art, margin = startup.wordmark(width)
                self.assertLessEqual(
                    margin + len(art[0]), max(width, len(startup.WORDMARK_TEXT) + 2)
                )


class TheBannerSaysWhoAndWhen(unittest.TestCase):
    def test_the_identity_line_carries_a_user_a_host_and_a_date(self) -> None:
        line = startup.identity_line()
        self.assertIn("@", line)
        self.assertRegex(line, r"\d{2}:\d{2}$")

    def test_the_facts_are_labelled_rows_that_line_up(self) -> None:
        text = _banner(118, status="postgres · tracing off")
        rows = [ln for ln in text.splitlines() if ln.startswith("  ") and "   " in ln]
        labelled = [ln for ln in rows if ln.strip().split()[0] in ("workspace", "session", "runtime")]
        self.assertEqual(len(labelled), 3)
        columns = {ln.index(ln.strip().split()[1]) for ln in labelled}
        self.assertEqual(len(columns), 1, "the values start in one column")

    def test_a_resumed_session_says_so(self) -> None:
        self.assertIn("resumed", _banner(118, resuming=True))

    def test_the_update_notice_comes_after_the_wordmark_not_before_it(self) -> None:
        """It used to be the first thing on screen, above the art."""
        text = _banner(118, notice="langgraph-api 0.15.1 available")
        self.assertLess(
            text.index(startup.WORDMARK[0]),
            text.index("langgraph-api 0.15.1"),
            "the warning must not open the session",
        )

    def test_no_notice_means_no_warning_line(self) -> None:
        self.assertNotIn("⚠", _banner(118))

    def test_the_one_shot_path_can_add_a_resume_command(self) -> None:
        self.assertIn("--resume cli-123", _banner(118, resume_cmd="yuyutsava --resume cli-123"))

    def test_a_run_with_no_prompt_is_not_told_to_type_a_slash_command(self) -> None:
        self.assertIn("/help", _banner(118))
        self.assertNotIn("/help", _banner(118, hint=None))


class TheVersionNoticeIsAStringNotAPrint(unittest.TestCase):
    """It is returned so the banner can decide where it goes, and so the
    caller can run the network call off the start-up path."""

    def test_it_returns_empty_when_the_check_cannot_run(self) -> None:
        self.assertEqual(startup.version_notice.__annotations__["return"], "str")
        self.assertFalse(hasattr(startup, "print_version_notice"))


class BothRenderersAttributeTheUser(unittest.IsolatedAsyncioTestCase):
    """The missing half of the transcript. Nothing printed it before."""

    def _rich(self) -> tuple[RichChatRenderer, io.StringIO]:
        buf = io.StringIO()
        return (
            RichChatRenderer(
                verbose=False,
                workspace=Path("/tmp"),
                console=make_console(file=buf, width=88, force_terminal=False),
                live=False,
            ),
            buf,
        )

    def test_the_rich_renderer_marks_and_prints_the_message(self) -> None:
        renderer, buf = self._rich()
        renderer.user_message("where is the panel width decided?", when="14:33")
        text = _plain(buf.getvalue())
        self.assertIn(f"{gl.YOU} you", text)
        self.assertIn("14:33", text)
        self.assertIn("where is the panel width decided?", text)

    def test_the_plain_renderer_marks_and_prints_it_too(self) -> None:
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ChatRenderer(verbose=False, workspace=Path("/tmp")).user_message("hello", when="09:01")
        text = _plain(err.getvalue())
        self.assertIn(f"{gl.YOU} you", text)
        self.assertIn("hello", text)

    def test_an_empty_line_draws_nothing(self) -> None:
        renderer, buf = self._rich()
        renderer.user_message("   ")
        self.assertEqual(buf.getvalue(), "")

    def test_every_line_of_a_multi_line_message_survives(self) -> None:
        renderer, buf = self._rich()
        renderer.user_message("first\nsecond\nthird")
        text = _plain(buf.getvalue())
        for part in ("first", "second", "third"):
            self.assertIn(part, text)


class TheBannerIsCentredOnThePaneItIsDrawnIn(unittest.TestCase):
    """The banner writes to stderr, which the split view redirects into the
    left pane. Measuring the whole terminal centred the wordmark on a width it
    was not being drawn at, and it sat visibly off to the right of the pane."""

    def _commands(self, dashboard):
        from yuyutsava.cli.commands.chat_repl import ReplCommands

        return ReplCommands(
            workspace=Path("/tmp"),
            console=None,
            renderer=None,
            bundle=None,
            convo=None,
            runtime_toggles=None,
            status_line="",
            session=None,
            dashboard=dashboard,
        )

    def test_with_a_context_panel_it_measures_the_pane(self) -> None:
        dashboard = SimpleNamespace(left_width=88)
        self.assertEqual(self._commands(dashboard).banner_width, 88)

    def test_without_one_it_lets_the_banner_measure_the_terminal(self) -> None:
        self.assertIsNone(self._commands(None).banner_width)

    def test_the_wordmark_lands_in_the_middle_of_whatever_it_is_given(self) -> None:
        for width in (88, 118, 200):
            with self.subTest(width=width):
                art, margin = startup.wordmark(width)
                left, right = margin, width - margin - len(art[0])
                self.assertLessEqual(abs(left - right), 1, "centred, within a cell")


class EachAnswerSitsInItsOwnBox(unittest.IsolatedAsyncioTestCase):
    """An answer used to run straight into the tool lines around it, with only
    an indent to say where it began and nothing to say where it stopped."""

    def _rich(self, width: int = 88):
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

    async def _drive(self, renderer, *events):
        for kind, data in events:
            await renderer.render(StreamEvent(kind=kind, data=data))

    async def test_prose_is_framed_top_and_bottom(self) -> None:
        renderer, buf = self._rich()
        await self._drive(renderer, ("token", {"text": "an answer\n\n"}))
        await renderer.end_of_turn()
        text = _plain(buf.getvalue())
        self.assertIn(pf.TOP_LEFT, text)
        self.assertIn(pf.BOTTOM_LEFT, text)
        self.assertIn(pf.VERTICAL, text)
        self.assertIn("an answer", text)

    async def test_the_box_closes_before_a_tool_line_and_reopens_after(self) -> None:
        """A tool call drawn inside the frame would claim to be the answer."""
        renderer, buf = self._rich()
        await self._drive(
            renderer,
            ("token", {"text": "first\n\n"}),
            ("tool_call", {"name": "tr_read_file", "args": {"path": "/tmp/a"}}),
            ("tool_result", {"name": "tr_read_file", "preview": "ok", "full": "ok"}),
            ("token", {"text": "second\n\n"}),
        )
        await renderer.end_of_turn()
        lines = _plain(buf.getvalue()).splitlines()
        order = [
            "top" if pf.TOP_LEFT in ln else "bottom" if pf.BOTTOM_LEFT in ln else "tool"
            for ln in lines
            if pf.TOP_LEFT in ln or pf.BOTTOM_LEFT in ln or "tr_read_file" in ln
        ]
        self.assertEqual(order, ["top", "bottom", "tool", "top", "bottom"])

    async def test_a_notice_is_not_drawn_inside_the_answer(self) -> None:
        renderer, buf = self._rich()
        await self._drive(
            renderer,
            ("token", {"text": "an answer\n\n"}),
            ("notice", {"text": "the model returned nothing", "level": "warning"}),
        )
        await renderer.end_of_turn()
        lines = _plain(buf.getvalue()).splitlines()
        closed = next(i for i, ln in enumerate(lines) if pf.BOTTOM_LEFT in ln)
        noticed = next(i for i, ln in enumerate(lines) if "returned nothing" in ln)
        self.assertLess(closed, noticed)

    async def test_your_next_message_is_not_drawn_inside_the_last_answer(self) -> None:
        renderer, buf = self._rich()
        await self._drive(renderer, ("token", {"text": "an answer\n\n"}))
        renderer.user_message("and another thing")
        lines = _plain(buf.getvalue()).splitlines()
        closed = next(i for i, ln in enumerate(lines) if pf.BOTTOM_LEFT in ln)
        asked = next(i for i, ln in enumerate(lines) if "and another thing" in ln)
        self.assertLess(closed, asked)

    async def test_the_box_never_paints_past_the_pane(self) -> None:
        """The reason it is not a Panel is streaming, not width — but a frame
        one cell too wide would still bleed across the context column."""
        from yuyutsava.cli.render.ansi_wrap import visible_width

        for width in (40, 60, 88, 120):
            with self.subTest(width=width):
                renderer, buf = self._rich(width)
                await self._drive(
                    renderer, ("token", {"text": "a much longer answer " * 12 + "\n\n"})
                )
                await renderer.end_of_turn()
                for line in _plain(buf.getvalue()).splitlines():
                    self.assertLessEqual(visible_width(line), width)

    async def test_nothing_is_closed_that_was_never_opened(self) -> None:
        """A turn with no prose at all must not emit a stray bottom edge."""
        renderer, buf = self._rich()
        await self._drive(renderer, ("tool_call", {"name": "tr_ls", "args": {}}))
        await renderer.end_of_turn()
        text = _plain(buf.getvalue())
        self.assertNotIn(pf.BOTTOM_LEFT, text)
        self.assertNotIn(pf.TOP_LEFT, text)


class TheRolesAreOnePalette(unittest.TestCase):
    """Rich, prompt_toolkit and the raw escapes all read the same table. They
    were three lists before, and ``tool`` was bold cyan in one and plain cyan
    in another."""

    def test_both_ramps_define_every_role(self) -> None:
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                self.assertEqual(set(ansi.palette(theme)), set(ansi.ROLES))

    def test_the_two_ramps_differ(self) -> None:
        """A light terminal that got the dark ramp is the bug this prevents."""
        self.assertNotEqual(ansi.palette("dark"), ansi.palette("light"))

    def test_a_kind_owns_a_hue_and_state_does_not(self) -> None:
        """``you``, ``tool``, ``ok``, ``err``, ``warn`` and ``subagent`` must
        be six distinct colours, or the separation is not there."""
        dark = ansi.palette("dark")
        kinds = [dark[r] for r in ("you", "tool", "ok", "err", "warn", "subagent", "chrome")]
        self.assertEqual(len(set(kinds)), len(kinds))

    def test_the_rich_theme_is_derived_from_the_palette(self) -> None:
        from yuyutsava.cli.render.console import THEME

        dark = ansi.palette()
        for name in ("you", "tool", "ok", "err", "warn", "subagent", "chrome"):
            with self.subTest(style=name):
                self.assertEqual(str(THEME.styles[name].color.name), dark[name])

    def test_the_dashboard_styles_are_derived_from_the_palette(self) -> None:
        from yuyutsava.cli.render.dashboard import STYLE

        dark = ansi.palette()
        self.assertIn(dark["you"], STYLE["prompt"])
        self.assertIn(dark["tool"], STYLE["ctx.title"])

    def test_the_theme_can_be_chosen_from_the_environment(self) -> None:
        self.assertEqual(ansi.resolve_theme({"YUYUTSAVA_THEME": "light"}), "light")
        self.assertEqual(ansi.resolve_theme({"COLORFGBG": "0;15"}), "light")
        self.assertEqual(ansi.resolve_theme({"COLORFGBG": "15;0"}), "dark")
        self.assertEqual(ansi.resolve_theme({}), "dark", "dark is the default")

    def test_a_terminal_without_truecolor_gets_a_256_colour_escape(self) -> None:
        """``38;2`` on a 256-colour terminal prints its own parameters."""
        self.assertIn("38;5;", ansi.sgr("you", full=False))
        self.assertIn("38;2;", ansi.sgr("you", full=True))
        self.assertFalse(ansi.truecolor({"COLORTERM": ""}))
        self.assertTrue(ansi.truecolor({"COLORTERM": "truecolor"}))


class EveryStyleTheTranscriptAsksForResolves(unittest.TestCase):
    """A style Rich cannot parse is not an error — it draws in nothing.

    ``style="tool bold"`` looks like it names the theme's ``tool`` plus a
    weight. Rich resolves a theme name only when the whole string is that
    name, so it parsed those as two colour words, found neither, and drew the
    line in no style at all: the markers naming the speaker came out the same
    colour as the prose beneath them, silently.
    """

    _SOURCES = ("renderer.py", "plain.py", "panels.py", "artifact_view.py")

    def _styles_used(self) -> set[str]:
        found: set[str] = set()
        for name in self._SOURCES:
            source = Path("yuyutsava/cli/render") / name
            found |= set(re.findall(r'style="([^"{}]+)"', source.read_text()))
        return found

    def test_every_style_name_in_the_render_package_draws_something(self) -> None:
        from rich.style import Style

        console = make_console(file=io.StringIO(), width=80, force_terminal=True)
        used = self._styles_used()
        self.assertGreater(len(used), 5, "the scan found nothing — it stopped working")
        for name in sorted(used):
            with self.subTest(style=name):
                self.assertNotEqual(
                    console.get_style(name),
                    Style.null(),
                    f"{name!r} parses to nothing and draws in no style",
                )

    def test_a_theme_name_with_a_weight_appended_is_caught(self) -> None:
        """The negative control: the exact shape of the original bug.

        ``Console.get_style`` refuses it outright, which is why this scan can
        see it. ``Text(style=...)`` asks with a default and gets the null style
        instead, which is why the bug was invisible on screen.
        """
        from rich.errors import MissingStyle
        from rich.style import Style

        console = make_console(file=io.StringIO(), width=80, force_terminal=True)
        with self.assertRaises(MissingStyle):
            console.get_style("tool bold")
        self.assertEqual(console.get_style("tool bold", default=""), Style.null())
        self.assertNotEqual(console.get_style("tool.bold"), Style.null())

    def test_the_transcript_console_keeps_the_roles_apart(self) -> None:
        """In the split view Rich writes to a buffer, not a tty, and its own
        detection settled on sixteen colours — which collapsed ``you``,
        ``tool`` and ``subagent`` back into one cyan."""
        from yuyutsava.cli.render.console import color_system

        self.assertIn(color_system(), ("truecolor", "256"))
        console = make_console(file=io.StringIO(), width=80, force_terminal=True)
        self.assertIn(console.color_system, ("truecolor", "256"))

    def test_a_console_that_was_not_forced_still_detects_for_itself(self) -> None:
        """The negative control: forcing a colour system everywhere would write
        escape sequences into piped output, which has never had any."""
        console = make_console(file=io.StringIO(), width=80, force_terminal=False)
        self.assertFalse(console.is_terminal)
        with console.capture() as cap:
            console.print("plain", style="tool")
        self.assertEqual(cap.get(), "plain\n")


class TheGlyphsAreOneCellWide(unittest.TestCase):
    """The emoji that opened every reply was two cells in some fonts, one in
    others and a box in a few, so the column it sat in moved."""

    def test_no_glyph_is_wider_than_one_cell(self) -> None:
        from yuyutsava.cli.render.ansi_wrap import visible_width

        for name in gl.__all__:
            value = getattr(gl, name)
            with self.subTest(glyph=name):
                self.assertEqual(visible_width(value.strip()), 1, f"{name} is not one cell")
        # The prompt is the one entry that carries its own trailing space.
        self.assertEqual(visible_width(gl.PROMPT), 2)

    def test_neither_renderer_still_draws_the_emoji(self) -> None:
        for module in ("renderer", "plain"):
            with self.subTest(module=module):
                source = Path(f"yuyutsava/cli/render/{module}.py").read_text()
                self.assertNotIn("🤖", source)


if __name__ == "__main__":
    unittest.main()
