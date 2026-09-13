"""The chat dashboard: a panel that cannot lie, bleed, or feed itself back.

The split view is the one place where a display bug becomes a correctness bug,
so these pin the properties that keep it honest:

* **every panel line is exactly the panel width** — a short line lets the
  transcript beside it show through, a long one overwrites it;
* **estimates are marked and measurements are not**, and an unpriced model says
  so rather than showing ``$0.00``;
* the transcript sink keeps ANSI intact, splits only on newlines, and is
  bounded — the alternate screen has no scrollback of its own, so this deque IS
  the history;
* only the **visible slice** is converted to fragments, because converting a
  5,000-line buffer per frame is O(everything);
* **nothing the panel shows re-enters the agent's context** — same invariant
  the artifact box has to hold.

Run:  .venv/bin/python test/cli/test_dashboard.py
"""

from __future__ import annotations

import io
import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.context_panel import (
    MAX_PANEL_WIDTH,
    MIN_DASHBOARD_COLS,
    MIN_PANEL_WIDTH,
    bar,
    context_report,
    fmt_cost,
    fmt_pct,
    fmt_tokens,
    panel_fragments,
    panel_width_for,
)
from yuyutsava.cli.render.dashboard import (
    TranscriptBuffer,
    display_mode,
    wide_enough,
)
from yuyutsava.context.meter import ContextSnapshot


def snap(**kw) -> ContextSnapshot:
    base = dict(
        role="cli", thread_id="t1", model="gemini-3.5-flash",
        max_input_tokens=1_000_000, compact_trigger_tokens=700_000,
        system_tokens=4_000, tools_tokens=23_600, memory_tokens=5_100,
        skills_tokens=2_700, messages_tokens=29_200,
        message_count=62, offloaded_digests=27, calibrated=True,
        call_no=12, input_tokens=41_087, output_tokens=64,
        cache_read_tokens=38_000, est_cost_usd=0.0032, priced=True,
        calls=12, session_input_tokens=412_000, session_output_tokens=1_200,
        session_cache_read_tokens=380_000, session_cost_usd=0.0412,
        compactions=0, offloads=27,
    )
    base.update(kw)
    return ContextSnapshot(**base)


def panel_text(s, width: int, **kw) -> str:
    return "".join(t for _style, t in panel_fragments(s, width=width, **kw))


def panel_lines(s, width: int, **kw) -> list[str]:
    return [ln for ln in panel_text(s, width, **kw).split("\n") if ln]


class Formatting(unittest.TestCase):
    def test_token_counts_stay_narrow_across_four_orders_of_magnitude(self):
        self.assertEqual(fmt_tokens(0), "0")
        self.assertEqual(fmt_tokens(934), "934")
        self.assertEqual(fmt_tokens(4_000), "4.0k")
        self.assertEqual(fmt_tokens(72_848), "72.8k")
        self.assertEqual(fmt_tokens(412_000), "412k")
        self.assertEqual(fmt_tokens(1_000_000), "1.0M")
        for n in (0, 1, 999, 1_000, 999_999, 1_000_000, 12_000_000):
            self.assertLessEqual(len(fmt_tokens(n)), 6, n)

    def test_negative_and_none_are_zero_not_a_crash(self):
        self.assertEqual(fmt_tokens(-5), "0")
        self.assertEqual(fmt_tokens(None), "0")

    def test_an_unpriced_model_never_shows_a_confident_zero(self):
        self.assertEqual(fmt_cost(0.0, False), "unpriced")
        self.assertEqual(fmt_cost(12.5, False), "unpriced")
        self.assertEqual(fmt_cost(0.0032, True), "$0.0032")
        self.assertEqual(fmt_cost(12.5, True), "$12.50")

    def test_a_small_nonzero_share_is_not_rounded_to_nothing(self):
        self.assertEqual(fmt_pct(0.0), "0%")
        self.assertEqual(fmt_pct(0.0001), "<1%")
        self.assertEqual(fmt_pct(0.07), "7%")
        self.assertEqual(fmt_pct(1.0), "100%")
        self.assertEqual(fmt_pct(5.0), "100%")

    def test_a_used_window_always_shows_at_least_one_filled_cell(self):
        # Rounding a real 7 % down to an empty bar would say "nothing is
        # used", which is the one thing the bar exists to deny.
        filled, empty = bar(0.0001, 20)
        self.assertEqual(filled, 1)
        self.assertEqual(filled + empty, 20)

    def test_a_window_short_of_full_never_looks_full(self):
        filled, empty = bar(0.999, 20)
        self.assertEqual(empty, 1)

    def test_full_is_full_and_empty_is_empty(self):
        self.assertEqual(bar(1.0, 10), (10, 0))
        self.assertEqual(bar(0.0, 10), (0, 10))


class PanelGeometry(unittest.TestCase):
    def test_every_line_is_exactly_the_panel_width(self):
        # Anything else and the transcript column beside it shows through.
        for width in (MIN_PANEL_WIDTH, 28, 34, MAX_PANEL_WIDTH):
            for s in (snap(), snap(compactions=3), snap(cache_read_tokens=0),
                      snap(priced=False), ContextSnapshot(), None):
                for kw in ({}, {"tool": "tr_run_python"},
                           {"status": "Running a_very_long_tool_name_here…"}):
                    lines = panel_lines(s, width, **kw)
                    widths = {len(ln) for ln in lines}
                    self.assertEqual(
                        widths, {width},
                        f"width={width} snap={s and s.call_no} kw={kw}: {widths}",
                    )

    def test_panel_width_is_clamped_to_a_readable_range(self):
        self.assertEqual(panel_width_for(80), MIN_PANEL_WIDTH)
        self.assertEqual(panel_width_for(400), MAX_PANEL_WIDTH)
        self.assertEqual(panel_width_for(136), 34)

    def test_a_long_label_trims_the_label_not_the_number(self):
        # The number is the content; a truncated "23.6k" is worse than useless.
        line = next(ln for ln in panel_lines(snap(), MIN_PANEL_WIDTH)
                    if "23.6k" in ln)
        self.assertIn("23.6k", line)
        self.assertEqual(len(line), MIN_PANEL_WIDTH)

    def test_a_narrow_terminal_is_not_split_at_all(self):
        self.assertFalse(wide_enough(80))
        self.assertFalse(wide_enough(MIN_DASHBOARD_COLS - 1))
        self.assertTrue(wide_enough(MIN_DASHBOARD_COLS))


class DisplayModeLadder(unittest.TestCase):
    """Which view you get, and why. Five conditions, three outcomes."""

    def _mode(self, **kw) -> str:
        base = dict(classic=False, is_tty=True, rich=True, enabled=True, wide=True)
        base.update(kw)
        return display_mode(**base)

    def test_a_capable_wide_terminal_gets_the_split_view(self):
        self.assertEqual(self._mode(), "dashboard")

    def test_classic_keeps_the_single_pane_transcript(self):
        self.assertEqual(self._mode(classic=True), "rich")

    def test_the_env_opt_out_keeps_the_single_pane_transcript(self):
        self.assertEqual(self._mode(enabled=False), "rich")

    def test_a_narrow_terminal_falls_back_rather_than_cramming(self):
        self.assertEqual(self._mode(wide=False), "rich")

    def test_piped_input_falls_back(self):
        # The split view owns the keyboard; without a tty on stdin there is
        # nothing to own.
        self.assertEqual(self._mode(is_tty=False), "rich")

    def test_no_rich_means_plain_whatever_else_is_asked_for(self):
        for kw in ({}, {"classic": True}, {"wide": False}, {"is_tty": False}):
            self.assertEqual(self._mode(rich=False, **kw), "plain")


class PanelHonesty(unittest.TestCase):
    def test_segment_rows_are_marked_as_estimates(self):
        text = panel_text(snap(), 34)
        for label in ("system prompt", "tool schemas", "memory", "skills",
                      "messages", "free"):
            row = next(ln for ln in text.split("\n") if ln.strip().startswith(label))
            self.assertIn("≈", row, label)

    def test_provider_numbers_are_not_marked(self):
        lines = panel_lines(snap(), 34)
        in_row = next(ln for ln in lines if ln.strip().startswith("in "))
        self.assertNotIn("≈", in_row)

    def test_an_uncalibrated_panel_says_so(self):
        self.assertIn("uncalibrated", panel_text(snap(calibrated=False), 34))
        self.assertNotIn("uncalibrated", panel_text(snap(calibrated=True), 34))

    def test_an_unpriced_model_reads_unpriced(self):
        text = panel_text(snap(priced=False, est_cost_usd=0.0), 34)
        self.assertIn("unpriced", text)
        self.assertNotIn("$0.00", text)

    def test_the_cache_share_is_hidden_when_none_was_reported(self):
        # 0 means "no cache detail", not a 0 % hit rate.
        self.assertIn("↺", panel_text(snap(cache_read_tokens=38_000), 34))
        self.assertNotIn("↺", panel_text(snap(cache_read_tokens=0,
                                              session_cache_read_tokens=0), 34))

    def test_an_imminent_compaction_is_called_out(self):
        hot = snap(messages_tokens=900_000)
        self.assertIn("compaction imminent", panel_text(hot, 34))
        self.assertNotIn("compaction imminent", panel_text(snap(), 34))

    def test_no_snapshot_says_so_rather_than_showing_zeros(self):
        text = panel_text(None, 34)
        self.assertIn("no model call yet", text)
        self.assertNotIn("0%", text)

    def test_the_tool_in_flight_is_shown(self):
        self.assertIn("tr_run_python", panel_text(snap(), 34, tool="tr_run_python"))


class ContextReport(unittest.TestCase):
    def _render(self, s, width: int = 76) -> str:
        buf = io.StringIO()
        console = make_console(file=buf, width=width, force_terminal=False)
        console.print(context_report(s, width=width))
        return buf.getvalue()

    def test_it_reports_the_window_the_segments_and_both_totals(self):
        out = self._render(snap())
        self.assertIn("gemini-3.5-flash", out)
        self.assertIn("1,000,000", out)
        for label in ("system prompt", "tool schemas", "memory", "skills",
                      "messages", "free"):
            self.assertIn(label, out)
        self.assertIn("41,087", out)   # last call, verbatim
        self.assertIn("412,000", out)  # session
        self.assertIn("27 tool results offloaded", out)

    def test_it_explains_which_numbers_are_estimates(self):
        out = self._render(snap())
        self.assertIn("≈ marks an estimate", out)
        # The rows are a breakdown of the total, not five loose guesses.
        self.assertIn("sum to the total", out)

    def test_it_names_the_basis_of_the_total(self):
        # "23.5k / 1.0M" printed above "in 27.6k" for one prompt was the bug;
        # the report has to say which of the two the total is.
        estimated = self._render(snap())
        self.assertIn("character estimate", estimated)
        measured = self._render(snap(anchored=True, window_measured=True))
        self.assertIn("provider's own count", measured)
        self.assertNotIn("character estimate", measured)

    def test_a_measured_total_carries_no_approximation_mark(self):
        out = self._render(snap(anchored=True, window_measured=True))
        self.assertIn("64,600 used", out)
        self.assertNotIn("≈64,600", out)

    def test_it_explains_an_unpriced_model(self):
        out = self._render(snap(priced=False))
        self.assertIn("model_prices.json", out)

    def test_it_names_the_read_back_path_after_a_compaction(self):
        # A user who sees "3 compactions" needs to know the turns are still
        # reachable, not assume they are gone.
        out = self._render(snap(compactions=3))
        self.assertIn("ctx_history", out)
        self.assertNotIn("ctx_history", self._render(snap(compactions=0)))

    def test_no_snapshot_is_a_sentence_not_a_traceback(self):
        out = self._render(None)
        self.assertIn("no model call", out)

    def test_a_zero_window_does_not_divide_by_it(self):
        self._render(ContextSnapshot())  # must not raise


class Transcript(unittest.TestCase):
    def test_it_keeps_ansi_and_splits_only_on_newlines(self):
        buf = TranscriptBuffer()
        buf.write("\x1b[36mhel")
        buf.write("lo\x1b[0m\nworld")
        self.assertEqual(buf.lines(), ["\x1b[36mhello\x1b[0m", "world"])

    def test_a_partial_line_is_visible_before_its_newline(self):
        # Rich emits escape sequences and text in separate writes; a streamed
        # reply would otherwise not appear until it ended.
        buf = TranscriptBuffer()
        buf.write("streaming")
        self.assertEqual(buf.lines(), ["streaming"])
        self.assertEqual(buf.line_count(), 1)

    def test_it_is_bounded(self):
        buf = TranscriptBuffer(max_lines=10)
        for i in range(100):
            buf.write(f"line {i}\n")
        self.assertEqual(len(buf.lines()), 10)
        self.assertEqual(buf.lines()[-1], "line 99")

    def test_it_reports_as_a_terminal_so_rich_keeps_colour(self):
        self.assertTrue(TranscriptBuffer().isatty())

    def test_it_refuses_to_hand_out_a_descriptor(self):
        # A subprocess inheriting this would write past the pane; failing
        # loudly beats a corrupted screen.
        with self.assertRaises(io.UnsupportedOperation):
            TranscriptBuffer().fileno()

    def test_writing_notifies_the_host_once_per_write(self):
        hits = []
        buf = TranscriptBuffer(on_write=lambda: hits.append(1))
        buf.write("a\n")
        buf.write("b\n")
        self.assertEqual(len(hits), 2)

    def test_a_raising_notifier_cannot_break_a_write(self):
        def boom():
            raise RuntimeError("repaint failed")

        buf = TranscriptBuffer(on_write=boom)
        buf.write("still recorded\n")
        self.assertEqual(buf.lines(), ["still recorded"])

    def test_clear_empties_it(self):
        buf = TranscriptBuffer()
        buf.write("gone\n")
        buf.clear()
        self.assertEqual(buf.lines(), [])

    def test_rich_renders_into_it_at_the_pane_width(self):
        buf = TranscriptBuffer()
        console = make_console(file=buf, width=40, force_terminal=True)
        console.print("x" * 100)
        lines = buf.lines()
        self.assertGreater(len(lines), 1, "long output did not wrap to the pane")
        for line in lines:
            # Stripped of ANSI, no line may exceed the pane.
            import re

            plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
            self.assertLessEqual(len(plain), 40)

    def test_colour_survives_the_sink(self):
        buf = TranscriptBuffer()
        console = make_console(file=buf, width=40, force_terminal=True)
        console.print("hello", style="accent")
        self.assertIn("\x1b[", "".join(buf.lines()))


class RenderingNeverFeedsBackIntoContext(unittest.TestCase):
    """The panel is display-only, like the artifact box.

    Its numbers describe the context window; putting them *into* the window
    would make the instrument change what it measures, and grow it every turn.
    """

    def test_the_panel_is_a_pure_function_of_a_snapshot(self):
        import inspect

        src = inspect.getsource(panel_fragments)
        for forbidden in ("StreamEvent", "messages", "append_system", "Message("):
            self.assertNotIn(forbidden, src)

    def test_the_report_is_a_pure_function_of_a_snapshot(self):
        import inspect

        src = inspect.getsource(context_report)
        for forbidden in ("StreamEvent", "append_system", "Message("):
            self.assertNotIn(forbidden, src)

    def test_the_meter_policy_returns_no_directive(self):
        # A Directive is how a policy injects text into the conversation. The
        # meter must never produce one.
        import asyncio

        from yuyutsava.context.config import ContextSettings
        from yuyutsava.context.meter import ContextMeterPolicy
        from yuyutsava.policy.types import Turn, Usage

        policy = ContextMeterPolicy(settings=ContextSettings(), role="cli")
        self.assertIsNone(asyncio.run(policy.before_model(Turn(thread_id="t"))))
        self.assertIsNone(asyncio.run(policy.after_model(
            Turn(thread_id="t", usage=Usage(input_tokens=10)))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
