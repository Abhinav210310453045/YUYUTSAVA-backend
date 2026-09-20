"""The two renderers are peers, and the ring behaves the same in both.

`R-SOL-01`: the rich renderer used to subclass the plain one. It overrode every
public method, so it inherited none of the behaviour and all twenty-two private
helpers — fifteen of which it had already replaced with its own. A fix to one of
those was invisible in the other, which is how `image` events came to be dropped
from the plain renderer (`R-BUG-03`).

These tests pin the arrangement that replaced it: a protocol both satisfy, a
payload ring both compose, and neither importing the other.
:mod:`test.cli.test_renderer_event_kinds` is the other half — the protocol
guarantees the *surface*, that one guarantees the *coverage*.
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest
from pathlib import Path

from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.protocol import ChatRendererProtocol
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.core.streaming import StreamEvent


def _renderers(console_file: io.StringIO | None = None) -> dict[str, object]:
    """One of each renderer.

    The rich one gets the project's own console — it draws with named styles
    ("chrome"), and a bare ``Console`` raises ``MissingStyle`` on the first one
    — writing into *console_file* so a test can read what it drew. ``live=False``
    because a transient Live region would fight that capture.
    """
    return {
        "plain": ChatRenderer(verbose=True, workspace=Path("/tmp")),
        "rich": RichChatRenderer(
            verbose=True,
            workspace=Path("/tmp"),
            console=make_console(
                file=console_file or io.StringIO(), width=100, force_terminal=False
            ),
            live=False,
        ),
    }


class BothAreRenderers(unittest.TestCase):
    def test_each_satisfies_the_protocol(self) -> None:
        for name, renderer in _renderers().items():
            with self.subTest(renderer=name):
                self.assertIsInstance(renderer, ChatRendererProtocol)

    def test_neither_is_a_subclass_of_the_other(self) -> None:
        """The point of the change: peers, not a base and a special case."""
        self.assertFalse(issubclass(RichChatRenderer, ChatRenderer))
        self.assertFalse(issubclass(ChatRenderer, RichChatRenderer))

    def test_the_rich_renderer_does_not_import_the_plain_one(self) -> None:
        source = Path("yuyutsava/cli/render/renderer.py").read_text()
        self.assertNotIn("from yuyutsava.cli.render.plain import", source)

    def test_something_missing_a_method_is_not_a_renderer(self) -> None:
        """Negative control: the protocol has to be able to say no."""

        class Halfway:
            ring = PayloadRing()
            on_change = None

            def begin_turn(self) -> None: ...
            def status_text(self) -> str:
                return ""

            # no render / end_of_turn / tool_in_flight / note_retry / pause / aclose

        self.assertNotIsInstance(Halfway(), ChatRendererProtocol)

    def test_the_repl_never_reaches_past_the_protocol(self) -> None:
        """Every renderer member the REPL touches must be on the protocol.

        The one that was not — `_smoother` — survived a refactor only because
        the call site sat inside `contextlib.suppress(Exception)`, so the
        AttributeError was swallowed and nothing said the teardown had stopped
        working.
        """
        source = Path("yuyutsava/cli/commands/chat_repl.py").read_text()
        private = sorted(set(re.findall(r"\brenderer\.(_\w+)", source)))
        self.assertEqual(private, [], f"chat_repl reaches for {private} on a renderer")


class TheRingIsShared(unittest.TestCase):
    """Both renderers must capture payloads identically, because `/expand <n>`
    is answered by whichever renderer happens to be running."""

    def test_both_capture_a_call_and_hand_back_an_index(self) -> None:
        for name, renderer in _renderers().items():
            with self.subTest(renderer=name):
                idx = renderer.ring.push("tool_call", "tr_read", '{"path": "/tmp/a"}')
                self.assertEqual(idx, 1, "indices start at 1 and are what /expand takes")
                entry = renderer.ring.find(idx)
                self.assertEqual(entry.name, "tr_read")
                self.assertFalse(entry.truncated)

    def test_an_oversized_body_is_capped_and_says_so(self) -> None:
        ring = PayloadRing(size=4, entry_cap=2048)
        idx = ring.push("tool_result", "tr_read", "x" * 5000)
        entry = ring.find(idx)
        self.assertTrue(entry.truncated)
        self.assertLess(len(entry.full), 5000)
        self.assertIn("truncated to 2KB", entry.full)

    def test_the_ring_forgets_the_oldest(self) -> None:
        ring = PayloadRing(size=2)
        for i in range(4):
            ring.push("tool_call", f"t{i}", "body")
        self.assertEqual(len(ring), 2)
        self.assertIsNone(ring.find(1), "an evicted index is gone, not an error")
        self.assertIsNotNone(ring.find(4))

    def test_printing_an_empty_ring_says_it_is_empty(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            PayloadRing().print_all()
            PayloadRing().print_last(3)
        self.assertEqual(err.getvalue().count("(ring empty)"), 2)

    def test_expanding_a_missing_entry_points_at_ring(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            PayloadRing().print_one(99)
        self.assertIn("no ring entry #99", err.getvalue())

    def test_a_stored_payload_can_be_read_back_whole(self) -> None:
        ring = PayloadRing()
        idx = ring.push("tool_result", "tr_read", "line one\nline two")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ring.print_one(idx)
        out = err.getvalue()
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_the_listing_flattens_newlines(self) -> None:
        """One line per entry, or a multi-line payload wrecks the listing."""
        ring = PayloadRing()
        ring.push("tool_call", "tr_read", "a\nb\nc")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ring.print_all()
        body = [ln for ln in err.getvalue().splitlines() if "[#1]" in ln]
        self.assertEqual(len(body), 1)
        self.assertIn("⏎", body[0])


class TheZoneChipAgrees(unittest.TestCase):
    """Both renderers draw a zone chip; they must not disagree about the zone."""

    def test_both_classify_the_same_path_the_same_way(self) -> None:
        rs = _renderers(io.StringIO())
        for path, expected in (
            ("/tmp/inside", "workspace"),
            ("/etc/hosts", "external"),
            ("relative.txt", ""),
        ):
            with self.subTest(path=path):
                plain_chip = rs["plain"]._zone_chip("tr_read", {"path": path})
                rich_zone = rs["rich"]._zone_name("tr_read", {"path": path})
                self.assertEqual(rich_zone, expected)
                if expected:
                    self.assertIn(expected, plain_chip)
                else:
                    self.assertEqual(plain_chip, "")

    def test_no_chip_for_a_tool_that_takes_no_path(self) -> None:
        rs = _renderers(io.StringIO())
        self.assertEqual(rs["plain"]._zone_chip("write_todos", {"path": "/etc"}), "")
        self.assertEqual(rs["rich"]._zone_name("write_todos", {"path": "/etc"}), "")

    def test_arguments_without_a_path_are_not_chipped(self) -> None:
        rs = _renderers(io.StringIO())
        for args in ({}, {"query": "q"}, "not a dict", None):
            with self.subTest(args=args):
                self.assertEqual(rs["plain"]._zone_chip("tr_read", args), "")
                self.assertEqual(rs["rich"]._zone_name("tr_read", args), "")


class TheWholeLifecycleRuns(unittest.IsolatedAsyncioTestCase):
    """Drive every protocol member on both renderers.

    Breaking the inheritance means the rich renderer no longer gets the plain
    one's ``__init__``, so an attribute it used to inherit and still reads would
    now raise at runtime — during a turn, in front of the user. The event-kind
    test covers ``render``; this covers everything around it, which nothing else
    calls.
    """

    async def test_no_renderer_is_missing_state_it_reads(self) -> None:
        for name, renderer in _renderers(io.StringIO()).items():
            with self.subTest(renderer=name):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    renderer.begin_turn()
                    self.assertIsInstance(renderer.status_text(), str)
                    self.assertIsInstance(renderer.tool_in_flight(), str)
                    renderer.note_retry("m", 1, 3, 2.0, RuntimeError("busy"))
                    with renderer.pause():
                        pass
                    await renderer.end_of_turn()
                    # Shutdown. The REPL used to reach for renderer._smoother
                    # here, a field only the plain renderer has, inside a
                    # blanket suppress that hid the AttributeError.
                    await renderer.aclose()

    async def test_a_verbose_call_and_result_round_trip(self) -> None:
        """The path that reads ring.entry_cap and the shared formatters — the
        four members the rich renderer used to inherit."""
        from yuyutsava.core.streaming import StreamEvent

        for name in ("plain", "rich"):
            with self.subTest(renderer=name):
                console_buf = io.StringIO()
                renderer = _renderers(console_buf)[name]
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    renderer.begin_turn()
                    await renderer.render(
                        StreamEvent(
                            kind="tool_call", data={"name": "tr_read", "args": {"path": "/tmp/a"}}
                        )
                    )
                    await renderer.render(
                        StreamEvent(
                            kind="tool_result",
                            data={"name": "tr_read", "preview": "ok", "full": "o" * 300_000},
                        )
                    )
                    await renderer.end_of_turn()
                self.assertEqual(len(renderer.ring), 2)
                stored = renderer.ring.find(2)
                self.assertTrue(stored.truncated, "a 300KB body must hit the per-entry cap")

    async def test_an_error_result_is_recognised_by_both(self) -> None:
        """tf.looks_like_error replaced a copy in each renderer; if one of them
        stopped recognising errors, failures would render as successes."""
        from yuyutsava.core.streaming import StreamEvent

        for name in ("plain", "rich"):
            with self.subTest(renderer=name):
                console_buf = io.StringIO()
                renderer = _renderers(console_buf)[name]
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    await renderer.render(
                        StreamEvent(kind="tool_call", data={"name": "tr_read", "args": {}})
                    )
                    await renderer.render(
                        StreamEvent(
                            kind="tool_result",
                            data={"name": "tr_read", "preview": '{"error":"nope"}'},
                        )
                    )
                    await renderer.end_of_turn()
                text = out.getvalue() + err.getvalue() + console_buf.getvalue()
                self.assertIn("\u2717", text, "an error result must be marked as one")


class ToolArgumentsCannotDistortTheLine(unittest.IsolatedAsyncioTestCase):
    """A tool argument is attacker-adjacent text: the agent may have been told
    to read a path, and what it was told comes from outside.

    The plain renderer used to summarise arguments with its own copy of
    `compact_summary`, which passed newlines and bidi overrides through. The
    terse view is one line per call, so a newline breaks the layout and a
    right-to-left override can reorder what a reader sees a path to be.
    """

    async def _summary_line(self, args: dict) -> str:
        # Not verbose: the terse one-line-per-call view is the one that
        # summarises arguments. Verbose prints them as indented JSON, where a
        # newline is expected and harmless.
        renderer = ChatRenderer(verbose=False, workspace=Path("/tmp"))
        console_buf = io.StringIO()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            await renderer.render(
                StreamEvent(kind="tool_call", data={"name": "tr_read", "args": args})
            )
        return out.getvalue() + err.getvalue() + console_buf.getvalue()

    async def test_a_newline_cannot_break_the_one_line_layout(self) -> None:
        text = await self._summary_line({"reason": "looks fine\nrm -rf /"})
        body = [ln for ln in text.splitlines() if "tr_read" in ln]
        self.assertEqual(len(body), 1, "the call must stay on one line")
        self.assertIn("rm -rf /", body[0], "and nothing is lost, only flattened")

    async def test_a_bidi_override_is_stripped(self) -> None:
        """U+202E reverses what follows it on screen."""
        text = await self._summary_line({"path": "/tmp/\u202egnp.exe"})
        self.assertNotIn("\u202e", text)

    async def test_a_zero_width_character_is_stripped(self) -> None:
        text = await self._summary_line({"path": "/tmp/a\u200bb"})
        self.assertNotIn("\u200b", text)


if __name__ == "__main__":
    unittest.main()
