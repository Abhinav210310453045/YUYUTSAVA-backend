"""Which rows of a growing transcript are on screen.

The transcript is stored as logical lines and displayed as wrapped rows, so
"scrolled back by N" is a position in *display* rows that only means anything
at a given width. All of that arithmetic is here, deliberately free of
prompt_toolkit: it takes three callables for the things that change underneath
it — the lines, the pane width, the pane height — and returns rows of text. The
application turns those into fragments.

Wrapping happens on read rather than on write so a narrower terminal re-wraps
what is already there instead of painting across the panel.
"""

from __future__ import annotations

from collections.abc import Callable

from yuyutsava.cli.render.ansi_wrap import HANG, row_count, wrap_ansi


class ScrollView:
    """A viewport over wrapped lines, with the tail as its home position."""

    def __init__(
        self,
        *,
        lines: Callable[[], list[str]],
        width: Callable[[], int],
        height: Callable[[], int],
        version: Callable[[], int] | None = None,
        hang: int = HANG,
    ) -> None:
        self._lines = lines
        self._width = width
        self._height = height
        #: Continuation indent for a line too long for the pane. Rich wraps its
        #: own output, so what reaches here is a ``print``, a log record or a
        #: warning — and those are the lines whose second row at column zero
        #: reads as a new statement rather than the rest of the one above.
        self._hang = hang
        #: A counter the content bumps whenever it changes. With one, the
        #: total row count is computed once per change instead of once per
        #: wheel tick — and a wheel sends several ticks a flick.
        self._version = version
        self._count_key: tuple[int, int] | None = None
        self._count = 0
        #: Display rows above the live tail. 0 means following the output,
        #: which is the position a new line must not move the reader away from.
        self.offset = 0

    # -- moving -------------------------------------------------------------

    def follow(self) -> None:
        """Return to the tail."""
        self.offset = 0

    def page(self) -> int:
        """A page, keeping one row of overlap so nothing is stepped over."""
        return max(1, self._height() - 1)

    def page_up(self) -> None:
        self.scroll_by(self.page())

    def page_down(self) -> None:
        self.scroll_by(-self.page())

    def scroll_by(self, rows: int) -> None:
        """Move by *rows*, clamped so the viewport cannot leave the content."""
        limit = max(0, self.display_count(self._width()) - self._height())
        self.offset = max(0, min(limit, self.offset + rows))

    # -- reading ------------------------------------------------------------

    def display_rows(self, need: int, width: int) -> list[str]:
        """The last *need* display rows, wrapping logical lines from the end.

        Walking backwards keeps the cost proportional to what is on screen
        rather than to the whole scrollback, which matters at a 5,000-line
        buffer and several frames a second.
        """
        out: list[str] = []
        for line in reversed(self._lines()):
            rows = wrap_ansi(line, width, hang=self._hang)
            out.extend(reversed(rows))
            if len(out) >= need:
                break
        out.reverse()
        return out[-need:] if len(out) > need else out

    def display_count(self, width: int) -> int:
        """Total display rows at this width.

        Called on every scroll key and every wheel tick. Memoised against the
        content's version and the width, so holding a scroll key costs one
        dictionary lookup rather than a re-wrap of the whole scrollback; the
        per-line counts are cached in :func:`row_count` underneath, so even a
        recount after new output is a walk of cached numbers.
        """
        if self._version is not None:
            key = (self._version(), width)
            if key == self._count_key:
                return self._count
            self._count_key = key
            self._count = sum(row_count(line, width, self._hang) for line in self._lines())
            return self._count
        return sum(row_count(line, width, self._hang) for line in self._lines())

    def visible(self) -> list[str]:
        """The rows the viewport is over, wrapped to the current width."""
        width = self._width()
        height = self._height()
        # display_rows already caps the result at height + offset, so dropping
        # the offset rows below the viewport leaves at most a screenful. A
        # further trim here would be unreachable.
        rows = self.display_rows(height + self.offset, width)
        if not self.offset:
            return rows
        return rows[: max(0, len(rows) - self.offset)]


__all__ = ["ScrollView"]
