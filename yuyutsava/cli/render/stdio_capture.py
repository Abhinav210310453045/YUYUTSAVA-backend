"""Everything the process writes, redirected into the transcript pane.

The split view takes the alternate screen, so anything that reaches the real
stdout or stderr lands on top of a repainting application. Three things write
there and each needs its own interception:

1. **``sys.stdout`` / ``sys.stderr``** — replaced with the buffer.
2. **Logging handlers.** A handler holds the stream object it was given, so
   replacing ``sys.stderr`` does not move it. Walking only the root logger is
   not enough either: ``cli.tooling.setup_logging`` attaches the CLI's handler
   to the ``yuyutsava`` logger with ``propagate=False``, so every warning in
   that tree keeps writing to the real stderr and lands on the prompt in
   yellow. Every logger with handlers is walked.
3. **``warnings``.** ``warnings.showwarning`` writes to the ``sys.stderr`` it
   captured earlier; Google's ``_CLOUD_SDK_CREDENTIALS_WARNING`` arrives that
   way and would be the first line on screen.

Out of reach: a child process that inherits fd 1/2 and writes to the tty
directly. It does not arise, because the ``tr_*`` tools capture their
subprocess output and return it as a tool result.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
import warnings
from collections import deque
from collections.abc import Callable
from typing import Any

from yuyutsava.cli.render.notices import warning_line

#: Lines of transcript kept for scrollback.
DEFAULT_SCROLLBACK = 5_000


class TranscriptBuffer:
    """File-like sink holding the transcript as pre-wrapped ANSI lines.

    Rich writes here instead of the terminal, so the left pane can display the
    result at whatever width it has. Accepts partial writes (Rich emits escape
    sequences and text separately) and only closes a line on ``\\n``.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(
        self,
        max_lines: int = DEFAULT_SCROLLBACK,
        on_write: Callable[[], None] | None = None,
    ) -> None:
        self._lines: deque[str] = deque(maxlen=max(1, max_lines))
        self._partial = ""
        self._on_write = on_write
        #: Bumped whenever the content changes. The view memoises its row
        #: count against this: without it, every wheel tick re-wrapped the
        #: whole scrollback to find out how far it was allowed to scroll.
        self._version = 0
        #: Provisional rows shown after the committed ones and replaced whole.
        #: Prose that has arrived but is not yet a complete markdown block
        #: lives here, so it is on screen while it is being written without
        #: ever entering the scrollback twice.
        self._tail: list[str] = []

    # -- file protocol ------------------------------------------------------

    def write(self, s: str) -> int:
        if not s:
            return 0
        text = self._partial + s
        *complete, self._partial = text.split("\n")
        self._lines.extend(complete)
        self._version += 1
        if self._on_write is not None:
            with contextlib.suppress(Exception):
                self._on_write()
        return len(s)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        # Rich checks this; the destination really is a terminal, one pane over.
        return True

    def fileno(self) -> int:
        # No descriptor to give. Anything reaching for one (a subprocess wanting
        # to inherit stdout) must fail loudly rather than write past the pane.
        raise io.UnsupportedOperation("TranscriptBuffer has no file descriptor")

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    # -- reading ------------------------------------------------------------

    def lines(self) -> list[str]:
        """Committed lines, the line still being written, then the tail."""
        out = list(self._lines)
        if self._partial:
            out.append(self._partial)
        out.extend(self._tail)
        return out

    def line_count(self) -> int:
        return len(self._lines) + (1 if self._partial else 0) + len(self._tail)

    def version(self) -> int:
        return self._version

    def set_tail(self, rows: list[str]) -> None:
        """Replace the provisional rows.

        Whole-list replacement rather than appending, because these rows are
        rewritten as more of the same paragraph arrives. They are dropped, not
        committed, when the finished block is printed properly — so nothing
        here can end up in the scrollback twice.
        """
        if rows == self._tail:
            return
        self._tail = list(rows)
        self._version += 1
        if self._on_write is not None:
            with contextlib.suppress(Exception):
                self._on_write()

    def clear(self) -> None:
        self._lines.clear()
        self._partial = ""
        self._tail = []
        self._version += 1


class StdioCapture:
    """Installs the redirection, and puts everything back on the way out.

    Separate from the application because it is process-global state with a
    strict acquire/release shape: whether it is installed has nothing to do
    with what the panes are drawing, and an application that failed to start
    must still be able to restore the terminal.
    """

    def __init__(self, buffer: TranscriptBuffer) -> None:
        self.buffer = buffer
        self._saved_streams: tuple[Any, Any] | None = None
        self._retargeted: list[tuple[Any, Any]] = []
        self._saved_showwarning: Any = None

    def install(self) -> None:
        """Route every write into the transcript pane.

        One interception instead of editing ~75 print sites — and it also
        catches the async-HITL notices written straight to stdout from a
        background task.

        Three separate write paths, all of which reached the real terminal and
        painted over a full-screen application:

        1. ``sys.stdout`` / ``sys.stderr`` — swapped.
        2. **``logging``.** ``StreamHandler`` binds its stream at construction,
           so existing handlers must be retargeted. The first version of this
           walked only the ROOT logger, and ``cli.tooling.setup_logging``
           attaches the CLI's handler to the ``yuyutsava`` logger with
           ``propagate=False`` — so every warning in the tree kept writing to
           the real stderr, landing on top of the prompt in yellow. Walk every
           logger that has handlers.
        3. **``warnings``.** ``warnings.showwarning`` writes to
           ``sys.stderr`` it captured earlier; Google's
           ``_CLOUD_SDK_CREDENTIALS_WARNING`` arrived that way and was the
           first line on screen. Redirected explicitly.

        What is genuinely out of reach: a child process that inherits fd 1/2
        and writes to the tty directly. It does not arise here because the
        ``tr_*`` tools capture their subprocess output and return it as a tool
        result rather than letting it through.
        """
        self._saved_streams = (sys.stdout, sys.stderr)
        old_out, old_err = self._saved_streams
        sys.stdout = self.buffer  # type: ignore[assignment]
        sys.stderr = self.buffer  # type: ignore[assignment]

        for logger_obj in self._all_loggers():
            for handler in list(getattr(logger_obj, "handlers", []) or []):
                stream = getattr(handler, "stream", None)
                if stream is old_out or stream is old_err:
                    self._retargeted.append((handler, stream))
                    with contextlib.suppress(Exception):
                        handler.setStream(self.buffer)  # type: ignore[attr-defined]

        self._saved_showwarning = warnings.showwarning
        warnings.showwarning = self._show_warning

    @staticmethod
    def _all_loggers() -> list[Any]:
        """Root plus every configured logger. Placeholders have no handlers."""
        out: list[Any] = [logging.getLogger()]
        manager = logging.getLogger().manager
        for obj in list(getattr(manager, "loggerDict", {}).values()):
            if isinstance(obj, logging.Logger):
                out.append(obj)
        return out

    def _show_warning(self, message, category, filename, lineno, file=None, line=None):
        """``warnings.showwarning`` replacement that writes into the pane.

        Formatted rather than passed through: nothing is filtered, so a real
        warning still arrives, but it arrives as one row in the transcript's
        own shape instead of two lines of absolute path at column zero.
        """
        with contextlib.suppress(Exception):
            self.buffer.write(warning_line(message, category, filename, lineno) + "\n")

    def restore(self) -> None:
        if self._saved_showwarning is not None:
            with contextlib.suppress(Exception):
                warnings.showwarning = self._saved_showwarning
            self._saved_showwarning = None
        for handler, stream in self._retargeted:
            with contextlib.suppress(Exception):
                handler.setStream(stream)  # type: ignore[attr-defined]
        self._retargeted.clear()
        if self._saved_streams is not None:
            sys.stdout, sys.stderr = self._saved_streams
            self._saved_streams = None

    def replay_to(self, stream: Any) -> None:
        """Write the transcript to *stream* — used when the app shuts down.

        The alternate screen takes the session's output with it when it exits.
        Reprinting the tail means a user who scrolled back through a long
        session still has it in their terminal afterwards.
        """
        with contextlib.suppress(Exception):
            for line in self.buffer.lines():
                stream.write(line + "\n")
            stream.flush()


__all__ = ["DEFAULT_SCROLLBACK", "StdioCapture", "TranscriptBuffer"]
