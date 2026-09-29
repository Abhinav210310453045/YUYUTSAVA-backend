"""A dotted frame drawn around the assistant's prose, one line at a time.

An answer used to run straight into the tool lines above and below it, with
only an indent to say where it started and nothing at all to say where it
stopped. A box says both.

It cannot be a :class:`rich.panel.Panel`, which is the obvious way to draw one:
a Panel has to know its full contents before it can close its bottom edge, and
this transcript commits each markdown block the moment that block is complete
(see :mod:`~yuyutsava.cli.render.markdown_stream`) precisely so a long answer
appears while it is being written rather than all at once at the end. So the
frame is drawn in three pieces — a top edge when prose opens, a rail down the
left and right of every committed line, and a bottom edge when something else
interrupts or the turn ends — and the streaming is unaffected.

Dotted rather than solid because the box is scenery: it separates the answer
from the machinery around it without competing with the answer.
"""

from __future__ import annotations

import io

from rich.console import Console, RenderableType
from rich.text import Text

from yuyutsava.cli.render.console import make_console

#: Left margin. The frame sits at the marker column, so the ``◇ yuyutsava``
#: line above it and the box below line up.
MARGIN = 2

#: Kept clear on the right so the frame never touches the pane's own edge.
RIGHT_GUTTER = 2

TOP_LEFT, TOP_RIGHT = "╭", "╮"
BOTTOM_LEFT, BOTTOM_RIGHT = "╰", "╯"
HORIZONTAL = "┄"
VERTICAL = "┊"

#: Cells the rail and its padding take from each side of a content line.
_RAIL = 2


class ProseFrame:
    """The three pieces of the box, and the width arithmetic behind them."""

    def __init__(self, console: Console, *, style: str) -> None:
        self._console = console
        self._style = style
        self._scratch_console: Console | None = None
        self.open = False

    # -- geometry -----------------------------------------------------------

    @property
    def width(self) -> int:
        """Outer width of the box, in cells."""
        return max(8, (self._console.width or 80) - MARGIN - RIGHT_GUTTER)

    @property
    def inner(self) -> int:
        """Width available to the content between the two rails."""
        return max(1, self.width - _RAIL * 2)

    # -- the three pieces ---------------------------------------------------

    def open_frame(self) -> None:
        if self.open:
            return
        self._edge(TOP_LEFT, TOP_RIGHT)
        self.open = True

    def close_frame(self) -> None:
        if not self.open:
            return
        self._edge(BOTTOM_LEFT, BOTTOM_RIGHT)
        self.open = False

    def write(self, renderable: RenderableType) -> None:
        """Draw *renderable* inside the rails, one row at a time."""
        for row in self._rows(renderable):
            self._console.print(row)

    def rows(self, renderable: RenderableType) -> list[str]:
        """The same rows, as ANSI strings, for a caller that shows them itself.

        Used for the part of an answer that has arrived but is not yet a
        complete markdown block: those rows are displayed under the frame's
        open top edge, replaced as more text arrives, and dropped when the
        finished block is printed properly — so a paragraph is on screen while
        it is being written without ever entering the scrollback twice.
        """
        return [self._to_ansi(row) for row in self._rows(renderable)]

    def _rows(self, renderable: RenderableType) -> list[Text]:
        """One :class:`Text` per display row, rails included.

        Rendered to segments rather than to a string so the markdown keeps its
        own styles — bold, code, list bullets — inside the frame.
        """
        options = self._console.options.update(width=self.inner)
        out: list[Text] = []
        for line in self._console.render_lines(renderable, options, pad=True):
            row = Text(" " * MARGIN)
            row.append(f"{VERTICAL} ", style=self._style)
            for segment in line:
                if not segment.control:
                    row.append(segment.text, segment.style)
            row.append(f" {VERTICAL}", style=self._style)
            out.append(row)
        return out

    def _to_ansi(self, row: Text) -> str:
        """A row as escape sequences, through Rich's own writer.

        A scratch console rather than rendering the segments by hand: turning
        a style into escapes means knowing which colour system is in play, and
        a second implementation of that is a second thing to get wrong.
        """
        scratch = self._scratch
        scratch.width = max(8, (self._console.width or 80) + MARGIN)
        with scratch.capture() as cap:
            scratch.print(row, end="", overflow="ignore", crop=False, no_wrap=True)
        return cap.get()

    @property
    def _scratch(self) -> Console:
        if self._scratch_console is None:
            self._scratch_console = make_console(
                file=io.StringIO(), width=self._console.width, force_terminal=True
            )
        return self._scratch_console

    def _edge(self, left: str, right: str) -> None:
        bar = HORIZONTAL * max(0, self.width - 2)
        self._console.print(Text(f"{' ' * MARGIN}{left}{bar}{right}", style=self._style))


__all__ = [
    "BOTTOM_LEFT",
    "BOTTOM_RIGHT",
    "HORIZONTAL",
    "MARGIN",
    "ProseFrame",
    "RIGHT_GUTTER",
    "TOP_LEFT",
    "TOP_RIGHT",
    "VERTICAL",
]
