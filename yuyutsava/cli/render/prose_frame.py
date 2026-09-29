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

from rich.console import Console, RenderableType
from rich.text import Text

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
        """Draw *renderable* inside the rails, one row at a time.

        Rendered to segments rather than to a string so the markdown keeps its
        own styles — bold, code, list bullets — inside the frame.
        """
        options = self._console.options.update(width=self.inner)
        for line in self._console.render_lines(renderable, options, pad=True):
            row = Text(" " * MARGIN)
            row.append(f"{VERTICAL} ", style=self._style)
            for segment in line:
                if not segment.control:
                    row.append(segment.text, segment.style)
            row.append(f" {VERTICAL}", style=self._style)
            self._console.print(row)

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
