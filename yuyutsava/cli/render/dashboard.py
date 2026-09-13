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
import time
import warnings
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.layout.controls import FormattedTextControl

from yuyutsava.cli.render.ansi_wrap import visible_width, wrap_ansi
from yuyutsava.cli.render.context_panel import (
    MIN_DASHBOARD_COLS,
    panel_fragments,
    panel_width_for,
)

logger = logging.getLogger("yuyutsava.cli.dashboard")

#: Lines of transcript kept. The alternate screen has no scrollback of its own,
#: so this IS the history the user can scroll through.
DEFAULT_SCROLLBACK = 5_000

#: How long a panel notice stays up, and how many can stack.
_NOTICE_TTL_SEC = 30.0
_NOTICE_MAX = 3

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


class _ScrollableTextControl(FormattedTextControl):
    """A text control whose wheel events scroll the owner instead of the window.

    ``FormattedTextControl`` only dispatches mouse events that were attached to
    individual fragments, and this pane's fragments come from parsed ANSI, so
    there is nothing to attach them to. Overriding the method is the documented
    way in; returning ``NotImplemented`` for anything else leaves clicks and
    selection to prompt_toolkit.
    """

    def __init__(self, text: Any, *, on_scroll: Callable[[int], None]) -> None:
        super().__init__(text)
        self._on_scroll = on_scroll

    def mouse_handler(self, mouse_event):  # noqa: ANN001, ANN201
        from prompt_toolkit.mouse_events import MouseEventType

        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll(3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll(-3)
            return None
        return NotImplemented


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
        self._saved_showwarning: Any = None
        #: (text, expiry) transient panel lines — see notice().
        self._notices: list[tuple[str, float]] = []
        self._said_unpriced = False
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

        self._out_control = _ScrollableTextControl(
            self._visible_transcript, on_scroll=self._on_wheel,
        )
        self._out_window = Window(
            content=self._out_control, wrap_lines=False, always_hide_cursor=True,
        )
        self._panel_window = Window(
            content=FormattedTextControl(self._panel_text),
            width=lambda: self._panel_width,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        panel = self._panel_window
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
            # prompt_toolkit enables these by default in full screen, and they
            # bind PgUp/PgDn against the FOCUSED window — the one-row input —
            # so the transcript could not be scrolled at all. This pane owns
            # its own scrolling.
            enable_page_navigation_bindings=False,
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

    def _on_wheel(self, lines: int) -> None:
        """Wheel over the transcript scrolls it.

        Handled on the control as well as via the ScrollUp/ScrollDown keys:
        terminals differ over whether a wheel arrives as a mouse event or as a
        synthetic key, and the wheel is how people actually scroll.
        """
        self._scroll_by(lines)
        self._invalidate()

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

    def _display_rows(self, need: int, width: int) -> list[str]:
        """The last *need* display rows, wrapping logical lines from the end.

        Wrapping happens here, not on write, so a narrower terminal re-wraps
        instead of painting across the panel. Walking backwards keeps the cost
        proportional to what is on screen rather than to the whole scrollback —
        which matters at a 5,000-line buffer and several frames a second.
        """
        out: list[str] = []
        for line in reversed(self.buffer.lines()):
            rows = wrap_ansi(line, width)
            out.extend(reversed(rows))
            if len(out) >= need:
                break
        out.reverse()
        return out[-need:] if len(out) > need else out

    def _display_count(self, width: int) -> int:
        """Total display rows at this width. Only called on a scroll key."""
        return sum(len(wrap_ansi(line, width)) for line in self.buffer.lines())

    def _scroll_by(self, lines: int) -> None:
        width = self.left_width
        limit = max(0, self._display_count(width) - self._height())
        self._scroll = max(0, min(limit, self._scroll + lines))

    def _visible_transcript(self):
        """The visible rows, wrapped to the pane and converted to fragments."""
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text

        self._sync_width()
        width = self.left_width
        height = self._height()
        rows = self._display_rows(height + self._scroll, width)
        # Scrolled back: drop the rows below the viewport.
        if self._scroll:
            rows = rows[: max(0, len(rows) - self._scroll)]
        visible = rows[-height:] if len(rows) > height else rows
        if not visible:
            return []
        try:
            return to_formatted_text(ANSI("\n".join(visible)))
        except Exception:  # noqa: BLE001 — never let a stray byte blank the pane
            return [("", "\n".join(visible))]

    def _panel_text(self):
        if not self._panel_visible:
            return []
        info = self._panel_window.render_info
        height = info.window_height if info is not None else None
        return panel_fragments(
            self._snapshot, width=self._panel_width, height=height,
            status=self._safe(self._status), tool=self._safe(self._tool),
            notices=self.notices(),
        )

    # -- notices ------------------------------------------------------------

    def notice(self, text: str, *, ttl: float = _NOTICE_TTL_SEC) -> None:
        """Show a transient line in the panel.

        Where a provider retry, an unpriced model or any other aside belongs:
        the user asked for these on the right, never over the transcript and
        never over the prompt. Deduplicated, because "no price for this model"
        said once is information and said per call is noise.
        """
        if not text:
            return
        now = time.monotonic()
        self._notices = [(t, e) for t, e in self._notices if e > now and t != text]
        self._notices.append((text, now + ttl))
        del self._notices[:-_NOTICE_MAX]
        self._invalidate()

    def notices(self) -> list[str]:
        now = time.monotonic()
        return [t for t, expiry in self._notices if expiry > now]

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
        # Say the unpriced-model fact here, once, instead of letting a logger
        # warning land in the transcript. The panel's cost row already reads
        # "unpriced"; this explains what to do about it.
        if snap is not None and snap.call_no and not snap.priced and not self._said_unpriced:
            self._said_unpriced = True
            self.notice(
                f"{snap.model or 'this model'} has no price entry — cost is "
                f"unknown, not zero. Add it to ~/.yuyutsava/model_prices.json",
                ttl=120.0,
            )
        self._invalidate()

    def note_retry(
        self, model: str, attempt: int, retries: int, delay: float, exc: BaseException
    ) -> None:
        """Provider-busy retries as a panel notice as well as a status line."""
        code = "429" if "ResourceExhausted" in type(exc).__name__ else "503"
        self.notice(f"provider busy ({code}) — retry {attempt}/{retries} in {delay:.0f}s",
                    ttl=max(5.0, delay + 5.0))

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
        catches the async-HITL notices written straight to stdout from a
        background task.

        Three separate write paths, all of which reached the real terminal and
        painted over a full-screen application:

        1. ``sys.stdout`` / ``sys.stderr`` — swapped.
        2. **``logging``.** ``StreamHandler`` binds its stream at construction,
           so existing handlers must be retargeted. The first version of this
           walked only the ROOT logger, and ``core.engine.setup_logging``
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
        """``warnings.showwarning`` replacement that writes into the pane."""
        with contextlib.suppress(Exception):
            text = warnings.formatwarning(message, category, filename, lineno, line)
            self.buffer.write(text if text.endswith("\n") else text + "\n")

    def _restore_streams(self) -> None:
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
