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
never land on top of a repainting region.

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
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
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
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import create_output
from prompt_toolkit.styles import Style

from yuyutsava.cli.line_reader import clear_line_reader, set_line_reader
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.context_panel import (
    MIN_DASHBOARD_COLS,
    panel_fragments,
    panel_width_for,
)
from yuyutsava.cli.render.input_broker import InputBroker
from yuyutsava.cli.render.protocol import ChatRendererProtocol
from yuyutsava.cli.render.scroll_view import ScrollView
from yuyutsava.cli.render.stdio_capture import (
    DEFAULT_SCROLLBACK,
    StdioCapture,
    TranscriptBuffer,
)
from yuyutsava.config.env import env_flag
from yuyutsava.context.meter import bus
from yuyutsava.llm.quirks.first_chunk_retry import classify_provider_busy

logger = logging.getLogger("yuyutsava.cli.dashboard")

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
    return env_flag("YUYUTSAVA_REPL_DASHBOARD", default=True, false=("0", "false", "no", "off"))


def terminal_cols() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 0


def wide_enough(cols: int | None = None) -> bool:
    cols = terminal_cols() if cols is None else cols
    return cols >= MIN_DASHBOARD_COLS


def display_mode(*, classic: bool, is_tty: bool, rich: bool, enabled: bool, wide: bool) -> str:
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
        self._cols = max(MIN_DASHBOARD_COLS, terminal_cols() or MIN_DASHBOARD_COLS)
        self._panel_width = panel_width_for(self._cols)
        self._panel_visible = True
        self._snapshot: Any | None = None
        self._status: Callable[[], str] = lambda: ""
        self._tool: Callable[[], str] = lambda: ""
        self._turn_task: asyncio.Task | None = None
        self._meter_token: int | None = None
        #: (text, expiry) transient panel lines — see notice().
        self._notices: list[tuple[str, float]] = []
        self._said_unpriced = False
        self._app_task: asyncio.Task | None = None

        self.buffer = TranscriptBuffer(scrollback, on_write=self._invalidate)
        self.capture = StdioCapture(self.buffer)
        self.input = InputBroker(
            history_path=history_path,
            completer=completer,
            invalidate=self._invalidate,
            is_closed=lambda: self._app_task is not None and self._app_task.done(),
        )
        self.view = ScrollView(
            lines=self.buffer.lines,
            width=lambda: self.left_width,
            height=self._height,
        )

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
        return make_console(file=self.buffer, width=self.left_width, force_terminal=True)

    @property
    def left_width(self) -> int:
        if not self._panel_visible:
            return max(20, self._cols)
        return max(20, self._cols - self._panel_width - 1)

    def _build_app(self, history_path: Path | None, completer: Any | None) -> None:
        self._out_control = _ScrollableTextControl(
            self._visible_transcript,
            on_scroll=self._on_wheel,
        )
        self._out_window = Window(
            content=self._out_control,
            wrap_lines=False,
            always_hide_cursor=True,
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

        left = HSplit(
            [
                self._out_window,
                Window(content=FormattedTextControl(self._status_text), height=1),
                VSplit(
                    [
                        Window(
                            content=FormattedTextControl(self.input.prompt_text),
                            width=lambda: len(self.input.prompt),
                            height=1,
                        ),
                        Window(content=BufferControl(buffer=self.input.buffer), height=1),
                    ]
                ),
            ]
        )
        body = VSplit(
            [
                left,
                ConditionalContainer(divider, panel_visible),
                ConditionalContainer(panel, panel_visible),
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                )
            ],
        )

        self._app = Application(
            layout=Layout(root, focused_element=self.input.buffer),
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
        """Hand the current line to the REPL.

        While a turn is running the REPL is not reading, so the line waits in
        the queue and is picked up the moment the turn ends. That is the safe
        default — it cannot kill a running tool call — but it is invisible, so
        the panel says so and offers Ctrl+S to interrupt.
        """
        # Any submission means "follow the output again": leaving the view
        # parked 200 lines up would hide the reply about to arrive.
        self.view.follow()
        queued = self.input.submit()
        task = self._turn_task
        if queued and queued.strip() and task is not None and not task.done():
            self.notice(
                "queued — sends when this turn ends. Ctrl+S to interrupt and send it now.",
                ttl=90.0,
            )

    def send_now(self) -> None:
        """Interrupt the running turn so a queued message goes immediately.

        Safe because the next turn repairs the interrupted tool call before it
        sends anything (conversation/repair.py) — the agent resumes with an
        accurate history rather than a fabricated success.
        """
        task = self._turn_task
        if task is None or task.done():
            return
        self.notice("interrupting — your message goes next", ttl=20.0)
        task.cancel()

    def interrupt(self) -> None:
        """Cancel the running turn, or clear the line when none is running.

        Raw mode means Ctrl+C never arrives as SIGINT, so the REPL's
        asyncio-cancellation path has to be triggered by hand. With no turn in
        flight this does what Ctrl+C does at a shell prompt.

        An outstanding question is refused first: Ctrl+C on a permission
        prompt must deny, and cancelling the turn while the prompt still waits
        would leave the tool call hanging on a future nobody resolves.
        """
        if self.input.has_pending_ask():
            self.input.clear_line()
            self.input.cancel_ask()
            return
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
        else:
            self.input.clear_line()

    def request_quit(self) -> None:
        """Ctrl+D — but only on an empty line, as in a shell."""
        if self.input.buffer.text:
            return
        # Quitting with a question open is a refusal, not an approval.
        self.input.cancel_ask()
        self.input.close()

    def follow(self) -> None:
        self.view.follow()

    def clear_transcript(self) -> None:
        self.buffer.clear()
        self.view.follow()

    def toggle_panel(self) -> None:
        self._panel_visible = not self._panel_visible
        self._sync_width(force=True)

    def page_up(self) -> None:
        self.view.page_up()

    def page_down(self) -> None:
        self.view.page_down()

    def _on_wheel(self, lines: int) -> None:
        """Wheel over the transcript scrolls it.

        Handled on the control as well as via the ScrollUp/ScrollDown keys:
        terminals differ over whether a wheel arrives as a mouse event or as a
        synthetic key, and the wheel is how people actually scroll.
        """
        self.view.scroll_by(lines)
        self._invalidate()

    def _key_bindings(self):
        kb = KeyBindings()

        def bind(key: Any, action: Callable[[], None]) -> None:
            kb.add(key)(lambda event: action())

        bind("enter", self.submit)
        bind("c-c", self.interrupt)
        bind("c-d", self.request_quit)
        bind("pageup", self.page_up)
        bind("pagedown", self.page_down)
        for key in ("s-up", "c-up"):
            bind(key, lambda: self.view.scroll_by(1))
        for key in ("s-down", "c-down"):
            bind(key, lambda: self.view.scroll_by(-1))
        bind(Keys.ScrollUp, lambda: self.view.scroll_by(3))
        bind(Keys.ScrollDown, lambda: self.view.scroll_by(-3))
        bind("end", self.follow)
        bind("c-l", self.clear_transcript)
        bind("c-g", self.toggle_panel)
        bind("c-s", self.send_now)
        return kb

    # -- rendering ----------------------------------------------------------

    def _height(self) -> int:
        info = self._out_window.render_info
        return info.window_height if info is not None else _FALLBACK_HEIGHT

    def _visible_transcript(self):
        """The visible rows as fragments. The slicing is the view's; turning
        ANSI into fragments is prompt_toolkit's, and so stays here."""
        self._sync_width()
        visible = self.view.visible()
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
            self._snapshot,
            width=self._panel_width,
            height=height,
            status=self._safe(self._status),
            tool=self._safe(self._tool),
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
        if self.view.offset:
            behind = f"  ↑{self.view.offset} lines back · End to follow"
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

    def attach_renderer(self, renderer: ChatRendererProtocol) -> None:
        """Take the status line from the renderer and repaint when it changes.

        These three members are the whole of what a dashboard needs from a
        renderer, which is why they are the protocol's data members: every
        renderer declares ``on_change``, so assigning it needs no guard.
        """
        self._status = lambda: renderer.status_text()
        self._tool = lambda: renderer.tool_in_flight()
        renderer.on_change = self._invalidate

    def set_turn_task(self, task: asyncio.Task | None) -> None:
        """The task Ctrl+C should cancel (the REPL's own, while a turn runs)."""
        self._turn_task = task

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Claim stdin for the application's lifetime. Every CLI prompt reads
        # through this seam, because a blocking input() cannot win a keystroke
        # against a full-screen application (see cli/line_reader.py).
        set_line_reader(self.ask)

        self.capture.install()
        self._meter_token = bus().subscribe("", self._on_snapshot)
        self._snapshot = bus().latest()
        self._app_task = asyncio.create_task(self._app.run_async(), name="yuyutsava-dashboard")
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
        code = classify_provider_busy(exc)
        self.notice(
            f"provider busy ({code}) — retry {attempt}/{retries} in {delay:.0f}s",
            ttl=max(5.0, delay + 5.0),
        )

    async def read_input(self) -> str | None:
        """Next submitted line, or ``None`` when the user asked to quit."""
        return await self.input.read_line()

    async def ask(self, prompt: str = "approve/reject> ") -> str | None:
        """Read one line as the answer to a question, not as a new turn.

        ``None`` means the user cancelled (Ctrl+C) or quit, which callers must
        treat as a refusal and never as consent.
        """
        return await self.input.ask(prompt)

    async def stop(self) -> None:
        # Release stdin before refusing the open question, so nothing new can
        # arrive in between.
        clear_line_reader(self.ask)

        if self._meter_token is not None:
            with contextlib.suppress(Exception):
                bus().unsubscribe(self._meter_token)
            self._meter_token = None
        # A shutdown with a question still open must not leave its tool call
        # waiting on a future that will never be resolved.
        with contextlib.suppress(Exception):
            self.input.cancel_ask()
        with contextlib.suppress(Exception):
            if self._app.is_running:
                self._app.exit()
        if self._app_task is not None:
            self._app_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._app_task
            self._app_task = None
        self.capture.restore()

    # -- handing the transcript back ---------------------------------------

    def replay_to(self, stream: Any) -> None:
        """Write the transcript to *stream* — used when the app shuts down.

        The alternate screen takes the session's output with it when it exits.
        Reprinting the tail means a user who scrolled back through a long
        session still has it in their terminal afterwards.
        """
        self.capture.replay_to(stream)

    # -- handing the transcript back ---------------------------------------


__all__ = [
    "DEFAULT_SCROLLBACK",
    "ChatDashboard",
    "TranscriptBuffer",
    "dashboard_enabled",
    "display_mode",
    "terminal_cols",
    "wide_enough",
]
