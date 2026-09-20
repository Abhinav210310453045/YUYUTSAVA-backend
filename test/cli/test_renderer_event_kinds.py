"""Every renderer handles every event kind.

`R-SOL-01`: there are two terminal renderers and no protocol between them, so
they drift silently — a kind the rich renderer draws and the plain one ignores
disappears from any piped transcript, with nothing to say it happened. That is
not hypothetical: `artifact` was already dropped here once, and the comment
explaining the repair still sits in the plain renderer.

The check is that a kind produces *some* output, not what it looks like. What
the two write is deliberately different — one is Rich on a console, the other
ANSI on stderr — and pinning the wording would only make this brittle.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from pathlib import Path

from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.core.streaming import StreamEvent

#: Every kind a turn can put on the wire, with a payload shaped like the real
#: one. Keep this in step with what `core/streaming` emits — a kind missing here
#: is a kind nothing checks.
#: Kinds that stand alone: each is one thing that happened, and each must leave
#: a mark of its own. Keep this in step with what `core/streaming` emits — a
#: kind missing here is a kind nothing checks.
DISCRETE: dict[str, dict] = {
    "tool_call": {"name": "tr_read_file", "args": {"path": "/tmp/x"}},
    "tool_result": {"name": "tr_read_file", "result": "contents"},
    "log": {"text": "preparing agent"},
    "notice": {"text": "heads up"},
    "image": {"title": "chart.png", "path": "/tmp/chart.png"},
    "artifact": {"title": "report", "kind": "markdown"},
}

#: `token` and `final` are not discrete: they are one prose stream and its
#: terminator. `final` deliberately writes only a newline — the tokens already
#: carried the text — so it is meaningless to assert on either alone.
PROSE = [("token", {"text": "the reply"}), ("final", {"text": "the reply"})]


def _drive(renderer, events: list[tuple[str, dict]]) -> str:
    """Render one event through a whole turn and return everything it wrote.

    The turn is closed rather than cut short, because prose is buffered: the
    rich renderer streams tokens into a markdown region that only flushes at
    the end, so rendering a `token` in isolation legitimately writes nothing.
    """
    buf = io.StringIO()

    async def _run() -> None:
        renderer.begin_turn()
        for kind, payload in events:
            await renderer.render(StreamEvent(kind, payload))
        await renderer.end_of_turn()

    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
        asyncio.run(_run())
    return buf.getvalue()


class EveryKindReachesEveryRenderer(unittest.TestCase):
    def _renderers(self, console_file: io.StringIO):
        # The project's own console: the rich renderer draws with named styles
        # ("chrome"), and a bare Console raises MissingStyle on the first one.
        return {
            "plain": ChatRenderer(verbose=True, workspace=Path("/tmp")),
            "rich": RichChatRenderer(
                verbose=True,
                workspace=Path("/tmp"),
                console=make_console(file=console_file, width=100, force_terminal=False),
            ),
        }

    def test_no_renderer_silently_drops_a_kind(self) -> None:
        for kind, payload in DISCRETE.items():
            for name in ("plain", "rich"):
                with self.subTest(renderer=name, kind=kind):
                    console_buf = io.StringIO()
                    renderer = self._renderers(console_buf)[name]
                    written = _drive(renderer, [(kind, payload)]) + console_buf.getvalue()
                    self.assertTrue(
                        written.strip(),
                        f"the {name} renderer wrote nothing for a {kind!r} event",
                    )

    def test_the_prose_stream_reaches_both(self) -> None:
        """Tokens plus their terminator, which is how prose actually arrives."""
        for name in ("plain", "rich"):
            with self.subTest(renderer=name):
                console_buf = io.StringIO()
                renderer = self._renderers(console_buf)[name]
                written = _drive(renderer, PROSE) + console_buf.getvalue()
                self.assertIn("the reply", written)

    def test_the_image_kind_names_the_visual(self) -> None:
        """The regression this file was written for.

        A generated chart that leaves no line in the transcript is
        indistinguishable from one the agent never made.
        """
        for name in ("plain", "rich"):
            with self.subTest(renderer=name):
                console_buf = io.StringIO()
                renderer = self._renderers(console_buf)[name]
                written = _drive(renderer, [("image", DISCRETE["image"])]) + console_buf.getvalue()
                self.assertIn("chart.png", written)


if __name__ == "__main__":
    unittest.main()
