"""Two-pane chat screen: transcript on the left, live context column on the right.

```
┌──────────── transcript (Rich, width-capped) ─────────────┬──── context ────┐
│  🤖 prose, ● tool lines, artifact boxes …                │ model / bar / % │
│                                                          │ segment rows    │
├──────────────────────────────────────────────────────────┤ last call       │
│ ✳ Running tr_run_python…                                 │ session totals  │
│ > ▏                                                      │ ● tool in flight│
└──────────────────────────────────────────────────────────┴─────────────────┘
```

## Why a full-screen application

A column that never scrolls away, with reply text that stops at its edge, is
not something a terminal offers in the normal buffer: there is a scroll region
for *rows* but nothing for columns, so anything painted at the right edge
scrolls up with the output and has to be repainted, leaving debris in the
scrollback. Taking the alternate screen is the only way to get a real one. The
cost is the terminal's own scrollback and mouse selection, so ``--classic``
(and ``YUYUTSAVA_REPL_DASHBOARD=0``) keeps the previous transcript REPL, and a
terminal narrower than ``MIN_DASHBOARD_COLS`` falls back to it automatically —
a 34-column panel out of 80 leaves 45 for prose, which turns ordinary markdown
into a column of fragments.

## How the existing renderer keeps working

Rich still renders everything. :class:`TranscriptBuffer` is a file-like sink,
``make_console(file=…, width=…)`` points the shared console at it, and the left
pane displays the ANSI it produces. ``RichChatRenderer``, ``MarkdownStream``,
the artifact panel and the ask cards are untouched — they simply wrap into the
left column and cannot cross into the panel.

The same sink is installed as ``sys.stdout``/``sys.stderr`` for the app's
lifetime, which is why none of the ~75 ``print(..., file=sys.stderr)`` calls in
``chat_repl`` had to change, and why ``CliRemoteHitl``'s mid-turn stdout writes
no longer land on top of a repainting region.

## Control flow

The application runs in its own task for the whole session; ``read_input``
awaits a queue that the Enter binding feeds. So the REPL loop keeps its
existing shape — read a line, run a turn, print a footer — while the panel
repaints throughout the turn. ``Ctrl+C`` cannot arrive as ``SIGINT`` here (the
terminal is in raw mode), so it is bound explicitly and cancels the turn task
the REPL registers.

## Known limit

Lines keep the width they were wrapped at, so a mid-session resize re-wraps
only new output. Re-rendering the scrollback would mean holding every Rich
renderable for the session — the same reasoning that kept the reply out of the
old ``Live`` region.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yuyutsava.cli.render.context_panel import (
    MIN_DASHBOARD_COLS,
    panel_fragments,
    panel_width_for,
)

logger = logging.getLogger("yuyutsava.cli.dashboard")

#: Lines of transcript kept. The alternate screen has no scrollback of its own,
#: so this IS the history the user can scroll through.
DEFAULT_SCROLLBACK = 5_000

#: Height of the left pane's fallback when prompt_toolkit has not rendered yet
#: (``render_info`` is None before the first frame) — one frame of slightly
#: wrong slicing, corrected immediately.
_FALLBACK_HEIGHT = 24

_STYLE = {
    "ctx.title": "bold cyan",
    "ctx.accent": "cyan",
    "ctx.dim": "#808080",
    "ctx.value": "bold",
    "ctx.warn": "bold yellow",
    "ctx.bar.fill": "cyan",
    "ctx.bar.empty": "#505050",
    "ctx.border": "#505050",
    "status": "#808080",
    "prompt": "bold cyan",
}


def dashboard_enabled() -> bool:
    """Whether the split view should be used at all.

    ``YUYUTSAVA_REPL_DASHBOARD=0`` opts out, same flag style as
    ``YUYUTSAVA_REPL_RICH``. A terminal too narrow to split is checked
    separately, at start, because it can change.
    """
    flag = os.environ.get("YUYUTSAVA_REPL_DASHBOARD", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def terminal_cols() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 0


def wide_enough(cols: int | None = None) -> bool:
    cols = terminal_cols() if cols is None else cols
    return cols >= MIN_DASHBOARD_COLS


def display_mode(
    *, classic: bool, is_tty: bool, rich: bool, enabled: bool, wide: bool
) -> str:
    """``"dashboard"`` | ``"rich"`` | ``"plain"`` — the whole fallback ladder.

    Extracted from the REPL so the decision is testable: five conditions
    feeding three outcomes is where a silent wrong choice hides, and "why did
    I get the plain renderer" is a question the code should be able to answer.
    """
    if not rich:
        return "plain"  # no TTY on stdout, or TERM=dumb
    if classic or not enabled or not is_tty or not wide:
        return "rich"
    return "dashboard"


class TranscriptBuffer:
    """File-like sink holding the transcript as pre-wrapped ANSI lines.

    Rich writes here instead of the terminal, so the left pane can display the
    result at whatever width it has. Accepts partial writes (Rich emits escape
    sequences and text separately) and only closes a line on ``\\n``.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(
        self, max_lines: int = DEFAULT_SCROLLBACK,
        on_write: Callable[[], None] | None = None,
    ) -> None:
        self._lines: deque[str] = deque(maxlen=max(1, max_lines))
        self._partial = ""
        self._on_write = on_write

    # -- file protocol ------------------------------------------------------

    def write(self, s: str) -> int:
        if not s:
            return 0
        text = self._partial + s
        *complete, self._partial = text.split("\n")
        self._lines.extend(complete)
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
        """Committed lines plus the line still being written."""
        out = list(self._lines)
        if self._partial:
            out.append(self._partial)
        return out

    def line_count(self) -> int:
        return len(self._lines) + (1 if self._partial else 0)

    def clear(self) -> None:
        self._lines.clear()
        self._partial = ""


class ChatDashboard:
    """Owns the application, the transcript sink and the Rich console."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        completer: Any | None = None,
        scrollback: int = DEFAULT_SCROLLBACK,
        output: Any | None = None,
        input: Any | None = None,  # noqa: A002 — prompt_toolkit's own name
    ) -> None:
        from prompt_toolkit.output import create_output

        self._cols = max(MIN_DASHBOARD_COLS, terminal_cols() or MIN_DASHBOARD_COLS)
        self._panel_width = panel_width_for(self._cols)
        self._panel_visible = True
        self._scroll = 0  # lines above the live tail; 0 == following output
        self._snapshot: Any | None = None
        self._status: Callable[[], str] = lambda: ""
        self._tool: Callable[[], str] = lambda: ""
        self._turn_task: asyncio.Task | None = None
        self._meter_token: int | None = None
        self._saved_streams: tuple[Any, Any] | None = None
        self._retargeted: list[tuple[Any, Any]] = []
        self._app_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

        self.buffer = TranscriptBuffer(scrollback, on_write=self._invalidate)

        # The real terminal, captured BEFORE sys.stdout is replaced — otherwise
        # the application would render into its own transcript pane. Overridable
        # so the whole application can be driven against a pty in a test, which
        # is the only way to check that what lands on screen is what the panel
        # says it should be.
        self._output = output or create_output(stdout=sys.__stdout__)
        self._input = input

        self.console = self._make_console()
        self._build_app(history_path, completer)

    # -- construction -------------------------------------------------------

    def _make_console(self):
        from yuyutsava.cli.render.console import make_console

        return make_console(file=self.buffer, width=self.left_width,
                            force_terminal=True)

    @property
    def left_width(self) -> int:
        if not self._panel_visible:
            return max(20, self._cols)
        return max(20, self._cols - self._panel_width - 1)

    def _build_app(self, history_path: Path | None, completer: Any | None) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            ConditionalContainer,
            Float,
            FloatContainer,
            HSplit,
            VSplit,
            Window,
        )
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.styles import Style

        history = (
            FileHistory(str(history_path)) if history_path is not None
            else InMemoryHistory()
        )
        self._buf = Buffer(
            history=history, completer=completer, complete_while_typing=True,
            multiline=False,
        )

        self._out_control = FormattedTextControl(self._visible_transcript)
        self._out_window = Window(
            content=self._out_control, wrap_lines=False, always_hide_cursor=True,
        )
        panel = Window(
            content=FormattedTextControl(self._panel_text),
            width=lambda: self._panel_width,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        divider = Window(width=1, char="│", style="class:ctx.border")
        panel_visible = Condition(lambda: self._panel_visible)

        left = HSplit([
            self._out_window,
            Window(content=FormattedTextControl(self._status_text), height=1),
            VSplit([
                Window(content=FormattedTextControl([("class:prompt", "> ")]),
                       width=2, height=1),
                Window(content=BufferControl(buffer=self._buf), height=1),
            ]),
        ])
        body = VSplit([
            left,
            ConditionalContainer(divider, panel_visible),
            ConditionalContainer(panel, panel_visible),
        ])
        root = FloatContainer(
            content=body,
            floats=[Float(xcursor=True, ycursor=True,
                          content=CompletionsMenu(max_height=8, scroll_offset=1))],
        )

        self._app = Application(
            layout=Layout(root, focused_element=self._buf),
            key_bindings=self._key_bindings(),
            style=Style.from_dict(_STYLE),
            full_screen=True,
            mouse_support=True,
            output=self._output,
            **({"input": self._input} if self._input is not None else {}),
            # The panel is driven by model calls, not by a clock; a periodic
            # refresh keeps a long provider wait from looking frozen without
            # repainting at spinner speed.
            refresh_interval=0.5,
        )

    # -- key actions --------------------------------------------------------
    #
    # Named methods rather than closures inside _key_bindings: what Ctrl+C
    # means here is behaviour worth testing on its own, and testing it through
    # the binding table means matching prompt_toolkit's normalised key names
    # (Enter is ControlM) instead of the behaviour.

    def submit(self) -> None:
        """Hand the current line to the REPL."""
        text = self._buf.text
        self._buf.reset(append_to_history=bool(text.strip()))
        # A new turn means "follow the output again": leaving the view parked
        # 200 lines up would hide the reply about to arrive.
        self._scroll = 0
        self._queue.put_nowait(text)

    def interrupt(self) -> None:
        """Cancel the running turn, or clear the line when none is running.

        Raw mode means Ctrl+C never arrives as SIGINT, so the REPL's
        asyncio-cancellation path has to be triggered by hand. With no turn in
        flight this does what Ctrl+C does at a shell prompt.
        """
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
        else:
            self._buf.reset()

    def request_quit(self) -> None:
        """Ctrl+D — but only on an empty line, as in a shell."""
        if self._buf.text:
            return
        self._queue.put_nowait(None)

    def follow(self) -> None:
        self._scroll = 0

    def clear_transcript(self) -> None:
        self.buffer.clear()
        self._scroll = 0

    def toggle_panel(self) -> None:
        self._panel_visible = not self._panel_visible
        self._sync_width(force=True)

    def page_up(self) -> None:
        self._scroll_by(self._page())

    def page_down(self) -> None:
        self._scroll_by(-self._page())

    def _key_bindings(self):
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        kb = KeyBindings()

        def bind(key: Any, action: Callable[[], None]) -> None:
            kb.add(key)(lambda event: action())

        bind("enter", self.submit)
        bind("c-c", self.interrupt)
        bind("c-d", self.request_quit)
        bind("pageup", self.page_up)
        bind("pagedown", self.page_down)
        for key in ("s-up", "c-up"):
            bind(key, lambda: self._scroll_by(1))
        for key in ("s-down", "c-down"):
            bind(key, lambda: self._scroll_by(-1))
        bind(Keys.ScrollUp, lambda: self._scroll_by(3))
        bind(Keys.ScrollDown, lambda: self._scroll_by(-3))
        bind("end", self.follow)
        bind("c-l", self.clear_transcript)
        bind("c-g", self.toggle_panel)
        return kb

    # -- rendering ----------------------------------------------------------

    def _height(self) -> int:
        info = self._out_window.render_info
        return info.window_height if info is not None else _FALLBACK_HEIGHT

    def _page(self) -> int:
        return max(1, self._height() - 1)

    def _scroll_by(self, lines: int) -> None:
        limit = max(0, self.buffer.line_count() - self._height())
        self._scroll = max(0, min(limit, self._scroll + lines))

    def _visible_transcript(self):
        """Only the visible slice, converted to fragments.

        Converting the whole scrollback each frame would be O(total chars) —
        5,000 lines at 10 fps — so the window is sliced before the ANSI is
        parsed. This is also why the lines are stored pre-wrapped.
        """
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text

        self._sync_width()
        lines = self.buffer.lines()
        height = self._height()
        end = max(0, len(lines) - self._scroll)
        start = max(0, end - height)
        visible = lines[start:end]
        if not visible:
            return []
        try:
            return to_formatted_text(ANSI("\n".join(visible)))
        except Exception:  # noqa: BLE001 — never let a stray byte blank the pane
            return [("", "\n".join(visible))]

    def _panel_text(self):
        if not self._panel_visible:
            return []
        return panel_fragments(
            self._snapshot, width=self._panel_width,
            status=self._safe(self._status), tool=self._safe(self._tool),
        )

    def _status_text(self):
        status = self._safe(self._status)
        if self._scroll:
            behind = f"  ↑{self._scroll} lines back · End to follow"
            return [("class:ctx.warn", (status + behind)[: self.left_width])]
        return [("class:status", status[: self.left_width])]

    @staticmethod
    def _safe(fn: Callable[[], str]) -> str:
        try:
            return fn() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _sync_width(self, *, force: bool = False) -> None:
        """Track terminal resizes: re-wrap new Rich output at the new width."""
        cols = 0
        with contextlib.suppress(Exception):
            cols = self._output.get_size().columns
        cols = cols or terminal_cols() or self._cols
        if not force and cols == self._cols:
            return
        self._cols = max(MIN_DASHBOARD_COLS, cols)
        self._panel_width = panel_width_for(self._cols)
        with contextlib.suppress(Exception):
            self.console.width = self.left_width

    def _invalidate(self) -> None:
        app = getattr(self, "_app", None)
        if app is not None and app.is_running:
            app.invalidate()

    # -- wiring -------------------------------------------------------------

    def attach_renderer(self, renderer: Any) -> None:
        """Take the status line from the renderer and repaint when it changes."""
        self._status = lambda: renderer.status_text()
        self._tool = lambda: renderer.tool_in_flight()
        with contextlib.suppress(AttributeError):
            renderer.on_change = self._invalidate

    def set_turn_task(self, task: asyncio.Task | None) -> None:
        """The task Ctrl+C should cancel (the REPL's own, while a turn runs)."""
        self._turn_task = task

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        from yuyutsava.context.meter import bus

        self._install_streams()
        self._meter_token = bus().subscribe("", self._on_snapshot)
        self._snapshot = bus().latest()
        self._app_task = asyncio.create_task(
            self._app.run_async(), name="yuyutsava-dashboard"
        )
        # Let the first frame render before the caller starts printing into it.
        await asyncio.sleep(0)

    def _on_snapshot(self, snap: Any) -> None:
        self._snapshot = snap
        self._invalidate()

    async def read_input(self) -> str | None:
        """Next submitted line, or ``None`` when the user asked to quit."""
        if self._app_task is not None and self._app_task.done():
            return None
        return await self._queue.get()

    async def stop(self) -> None:
        from yuyutsava.context.meter import bus

        if self._meter_token is not None:
            with contextlib.suppress(Exception):
                bus().unsubscribe(self._meter_token)
            self._meter_token = None
        with contextlib.suppress(Exception):
            if self._app.is_running:
                self._app.exit()
        if self._app_task is not None:
            self._app_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._app_task
            self._app_task = None
        self._restore_streams()

    # -- stdout / stderr capture -------------------------------------------

    def _install_streams(self) -> None:
        """Route every write into the transcript pane.

        One interception instead of editing ~75 print sites — and it also
        catches library logging and the async-HITL notices that are written
        straight to stdout from a background task.

        ``logging.StreamHandler`` binds its stream at construction, so handlers
        that already exist have to be retargeted explicitly; they are restored
        on the way out.
        """
        self._saved_streams = (sys.stdout, sys.stderr)
        sys.stdout = self.buffer  # type: ignore[assignment]
        sys.stderr = self.buffer  # type: ignore[assignment]
        old_out, old_err = self._saved_streams
        for handler in list(logging.getLogger().handlers):
            stream = getattr(handler, "stream", None)
            if stream is old_out or stream is old_err:
                self._retargeted.append((handler, stream))
                with contextlib.suppress(Exception):
                    handler.setStream(self.buffer)  # type: ignore[attr-defined]

    def _restore_streams(self) -> None:
        for handler, stream in self._retargeted:
            with contextlib.suppress(Exception):
                handler.setStream(stream)  # type: ignore[attr-defined]
        self._retargeted.clear()
        if self._saved_streams is not None:
            sys.stdout, sys.stderr = self._saved_streams
            self._saved_streams = None

    # -- handing the transcript back ---------------------------------------

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


__all__ = [
    "DEFAULT_SCROLLBACK",
    "ChatDashboard",
    "TranscriptBuffer",
    "dashboard_enabled",
    "display_mode",
    "terminal_cols",
    "wide_enough",
]
