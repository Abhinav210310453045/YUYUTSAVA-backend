"""RichChatRenderer — Claude-Code-style transcript renderer.

One of the two renderers behind
:class:`~yuyutsava.cli.render.protocol.ChatRendererProtocol`, for a TTY. It
shares the payload ring and the small formatters with the plain renderer by
composition — :mod:`~yuyutsava.cli.render.ring`,
:mod:`~yuyutsava.cli.render.tool_format` and
:mod:`~yuyutsava.cli.render.zones` — and owns its whole display path:

* one transient ``Live`` spinner per turn (``✳ Thinking…`` → ``Running
  <tool>…`` → streaming prose tail), everything else printed *above* it;
* ``● tool(arg-summary)`` lines the moment a call starts, append-only
  ``⎿ ✓ outcome`` lines when results land (parallel calls and
  interleaved tokens make in-place cursor edits unsafe);
* assistant prose committed block-by-block as rendered markdown;
* ``task`` subagents get a distinct ``◆`` header and their final
  response rendered as an indented markdown block.

Everything renders to a single stdout console — a Live region cannot
interleave safely across two streams, so rich mode drops the plain
renderer's stdout/stderr split.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from yuyutsava.cli.render import artifact_view as av
from yuyutsava.cli.render import glyphs as gl
from yuyutsava.cli.render import pulse, zones
from yuyutsava.cli.render import tool_format as tf
from yuyutsava.cli.render.markdown_stream import MarkdownStream
from yuyutsava.cli.render.notices import action_hint, notice_mark
from yuyutsava.cli.render.prose_frame import ProseFrame
from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.cli.stream_smoother import TokenSmoother, configured_cps, smoothing_enabled
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.llm.quirks.first_chunk_retry import classify_provider_busy

logger = logging.getLogger("yuyutsava.cli.render.renderer")

_SUBAGENT_TOOLS = ("task", "start_async_task")

#: The indent ladder. Column 2 is where a marker names who is speaking, and
#: column 4 is what they said — prose, your message, a tool's result.
_MARKER_INDENT = 2
_PROSE_INDENT = 4

# Cap for inline subagent-response rendering; /expand shows the rest.
_SUBAGENT_INLINE_CHARS = 4000


class _Pulse:
    """The moving mark and its label, re-read from the clock on every frame.

    A Rich renderable rather than a prepared ``Text`` because ``Live`` renders
    the object it was handed once per refresh: a string built in advance is the
    same string every tick, which is how a spinner ends up standing still.
    """

    def __init__(self, state: str, label: Text) -> None:
        self._state = state
        self._label = label

    def __rich_console__(self, console: Console, options: Any) -> Any:
        now = time.monotonic()
        line = Text(pulse.mark_at(now, self._state) + " ", style=pulse.hue_at(now))
        line.append_text(self._label)
        yield line


class RichChatRenderer:
    """The TTY renderer: a Live status region, Markdown prose, Rich panels.

    Satisfies :class:`~yuyutsava.cli.render.protocol.ChatRendererProtocol`.
    It shares the payload ring and the small formatters with the plain
    renderer by composition, and defines its whole display path itself.
    """

    def __init__(
        self,
        *,
        verbose: bool,
        workspace: Path | None,
        console: Console,
        live: bool = True,
    ) -> None:
        self._verbose = verbose
        self._workspace = workspace
        #: Shared with the plain renderer by composition, not inheritance.
        self.ring = PayloadRing()
        self._console = console
        #: The dotted box an answer is drawn inside. Three pieces rather
        #: than a Panel, because a Panel cannot close an edge it has not seen
        #: the end of, and prose is committed block by block as it arrives.
        self._frame = ProseFrame(console, style="agent")
        #: The same box around what you typed, in your own colour rather than
        #: the assistant's, so which side of the conversation a block belongs
        #: to is legible without reading the marker above it.
        self._you_frame = ProseFrame(console, style="you")
        self._md = MarkdownStream(console, indent=_PROSE_INDENT, frame=self._frame)
        #: Paces prose to a steady rate instead of painting each arriving
        #: chunk whole. The model delivers a paragraph in one burst after a
        #: stall, and mirroring that reads as the program stuttering rather
        #: than as the model thinking. ``None`` when the destination is not an
        #: interactive terminal, so piped output is unchanged.
        self._smoother = (
            TokenSmoother(self._feed_prose, base_cps=configured_cps())
            if smoothing_enabled()
            else None
        )
        #: Where the provisional rows go — the part of an answer that has
        #: arrived but is not yet a complete markdown block. Set by the host
        #: that can redraw them in place (the dashboard); without one, the
        #: in-progress text is only visible on the Live status region.
        self.show_pending: Any = None
        self._live: Live | None = None
        self._in_flight: deque[str] = deque()
        self._opened_prose = False
        self._sub_activity = ""  # last line of nested subagent prose
        self._retry_note = ""  # provider-busy banner while a 429 is retried
        #: False under the chat dashboard, which draws the status itself: a
        #: transient Live region inside a full-screen application would fight
        #: its renderer for the cursor.
        self._use_live = live
        #: Called whenever the status changes, so a host that draws its own
        #: status can repaint. Set by ChatDashboard.attach_renderer.
        self.on_change: Any = None
        #: Whether a turn is in progress. The spinner's existence used to carry
        #: this; the dashboard has no spinner and still needs to know whether
        #: "nothing is happening" means idle or waiting on the model.
        self._in_turn = False

    # ------------------------------------------------------------------
    # Turn / Live lifecycle
    # ------------------------------------------------------------------

    def note_retry(
        self, model: str, attempt: int, retries: int, delay: float, exc: BaseException
    ) -> None:
        """Show a provider retry on the spinner rather than as log noise.

        A 429 storm used to reach the user as a dozen structlog lines printed
        straight through the Live region, none of which said the turn was
        still alive. This keeps it to one line of spinner text that names the
        wait, and it clears itself as soon as real output arrives.
        """
        code = classify_provider_busy(exc)
        self._retry_note = f"provider busy ({code}) · retry {attempt}/{retries} · {delay:.0f}s"
        self._refresh()

    def user_message(self, text: str, *, when: str = "") -> None:
        """Put what you typed into the transcript, above the reply to it.

        Nothing printed it before. In the split view prompt_toolkit owns the
        screen and ``InputBroker.submit`` resets the buffer, so the line was
        erased the moment it was sent and the transcript read as an
        unattributed monologue — and because it never reached the transcript
        buffer it was missing from the replay printed back to the scrollback
        on exit, too.
        """
        text = (text or "").strip()
        if not text:
            return
        self._close_prose()
        stamp = when or _dt.datetime.now().strftime("%H:%M")
        head = Text(" " * _MARKER_INDENT)
        head.append(f"{gl.YOU} you:", style="you.bold")
        self._console.print()
        self._console.print()
        self._console.print(self._with_tail(head, "", stamp))
        self._you_frame.open_frame()
        self._you_frame.write(Text(text))
        self._you_frame.close_frame()
        self._notify()

    def skills_recalled(self, names: tuple[str, ...]) -> None:
        """One chrome line naming the skills this turn matched."""
        if not names:
            return
        self._close_prose()
        head = f"  {gl.SKILLS} skills  "
        shown, rest = tf.fit_names(names, max(8, (self._console.width or 80) - len(head)))
        line = Text(head, style="chrome")
        line.append(shown, style="chrome")
        if rest:
            line.append(f"  +{rest} more", style="chrome")
        self._console.print()
        self._console.print(line)

    def begin_turn(self) -> None:
        """Start the spinner. Called by the REPL right before run_turn."""
        self._stop_live()
        self._opened_prose = False
        self._sub_activity = ""
        self._retry_note = ""
        self._in_flight.clear()
        self._in_turn = True
        if not self._use_live:
            self._notify()
            return
        self._start_live()

    async def end_of_turn(self) -> None:
        self._close_prose()
        self._md.started = False
        self._stop_live()
        self._in_flight.clear()
        self._sub_activity = ""
        self._retry_note = ""
        self._in_turn = False
        self._notify()

    async def aclose(self) -> None:
        """Stop the prose smoother, if this terminal is being paced."""
        if self._smoother is not None:
            await self._smoother.aclose()
            self._smoother = None

    @contextlib.contextmanager
    def pause(self):
        """Stop the Live region around blocking input (ask cards).

        A no-op under the dashboard: ask cards render into the transcript pane,
        so there is no repainting region to get out of the way of.
        """
        had_live = self._live is not None
        if self._smoother is not None:
            self._smoother.flush_now()
        self._stop_live()
        try:
            yield
        finally:
            if had_live:
                self._start_live()

    def _start_live(self) -> None:
        self._live = Live(
            self._status_renderable(),
            console=self._console,
            transient=True,
            refresh_per_second=10,
        )
        self._live.start()

    def _stop_live(self) -> None:
        if self._live is not None:
            with contextlib.suppress(Exception):
                self._live.stop()
            self._live = None

    def _notify(self) -> None:
        if self.on_change is not None:
            with contextlib.suppress(Exception):
                self.on_change()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._status_renderable())
        self._notify()

    # ------------------------------------------------------------------
    # Status (one source, two presentations)
    # ------------------------------------------------------------------

    def _activity(self) -> tuple[str, str]:
        """What the agent is doing, as ``(state, detail)``.

        The one decision both presentations format — the spinner and the plain
        status line — so they cannot drift apart. ``state`` is ``retry``,
        ``subagent``, ``tool``, ``writing`` or ``thinking``.
        """
        if self._retry_note:
            return "retry", self._retry_note
        if self._in_flight:
            name = self._in_flight[-1]
            if name in _SUBAGENT_TOOLS:
                n = len(self._in_flight)
                return "subagent", f"({n} in flight)" if n > 1 else ""
            return "tool", name
        return ("writing", "") if self._md.started else ("thinking", "")

    def status_text(self) -> str:
        """The status line as plain text, for a host that draws its own status
        bar (the dashboard) — the same state the spinner shows."""
        state, detail = self._activity()
        if state == "retry":
            return detail
        if state == "subagent":
            return f"Subagent working… {detail}".rstrip()
        if state == "tool":
            return f"Running {detail}…"
        if state == "writing":
            return "Writing…"
        return "Thinking…" if self._in_turn else ""

    def status_state(self) -> str:
        """The phase :meth:`status_text` is describing, or ``""`` when idle."""
        if not self._in_turn:
            return ""
        return self._activity()[0]

    def tool_in_flight(self) -> str:
        """Name of the most recent unfinished tool call, or ``""``."""
        return self._in_flight[-1] if self._in_flight else ""

    def _status_renderable(self) -> RenderableType:
        state, detail = self._activity()
        if state == "retry":
            label = Text(detail, style="warn")
        elif state == "subagent":
            label = Text.assemble(("Subagent working… ", "subagent"), (detail, "chrome"))
        elif state == "tool":
            label = Text.assemble(("Running ", "chrome"), (detail, "tool"), ("…", "chrome"))
        elif state == "writing":
            label = Text("Writing…", style="chrome")
        else:
            label = Text("Thinking…", style="chrome")

        parts: list[RenderableType] = [_Pulse(state, label)]
        tail = self._md.tail() if self._md.started else ""
        if tail:
            parts.append(Padding(Text(tail, style="chrome", no_wrap=False), (0, 0, 0, 2)))
        elif self._sub_activity:
            parts.append(Padding(Text(self._sub_activity, style="chrome"), (0, 0, 0, 2)))
        return Group(*parts)

    # ------------------------------------------------------------------
    # Event rendering
    # ------------------------------------------------------------------

    async def render(self, ev: StreamEvent) -> None:
        # Any real output means the retried call got through; drop the banner.
        if self._retry_note:
            self._retry_note = ""
        data = ev.data
        match ev.kind:
            case "token":
                self._on_token(data)
            case "tool_call":
                self._on_tool_call(data)
            case "tool_result":
                self._on_tool_result(data)
            case "log":
                if text := data.get("text", ""):
                    self._console.print(Text(text, style="warn"))
            case "notice":
                self._on_notice(data)
            case "image":
                title = data.get("title") or data.get("path") or "image"
                self._console.print(
                    Text(f"  {gl.ARTIFACT} visual: {tf.sanitize(title)}", style="chrome")
                )
            case "artifact":
                self._print_artifact(data)
            case "final":
                self._close_prose()

    def _close_prose(self) -> None:
        """Commit what is buffered and close the box, if one is open.

        Everything that is not prose calls this first: a tool line, a notice
        or the next message drawn *inside* the answer's frame would say they
        were part of the answer.
        """
        if self._smoother is not None:
            # Synchronously, not awaited: a tool line or a notice must not
            # appear above prose the model produced before it, and there is no
            # awaiting a background task from a synchronous printer.
            self._smoother.flush_now()
        self._md.flush()
        self._show_pending()
        self._frame.close_frame()

    def _on_token(self, data: dict) -> None:
        text = data.get("text", "")
        if not text:
            return
        if self._is_subagent_token(data):
            last = text.strip().splitlines()
            if last:
                self._sub_activity = tf.truncate(tf.sanitize(last[-1]), 80)
            self._refresh()
            return
        if not self._opened_prose:
            self._console.print()
            self._console.print(Text(f"  {gl.AGENT} yuyutsava:", style="agent.bold"))
            self._opened_prose = True
        self._frame.open_frame()
        if self._smoother is not None:
            self._smoother.feed(text)
        else:
            self._feed_prose(text)
        self._refresh()

    def _feed_prose(self, text: str) -> None:
        """One paced slice of prose: commit what completes, show the rest."""
        self._md.feed(text)
        self._show_pending()

    def _show_pending(self) -> None:
        """Put the incomplete block on screen, or take it off again."""
        if self.show_pending is None:
            return
        rows = (
            self._frame.rows(Text(self._md.pending))
            if self._frame.open and self._md.pending.strip()
            else []
        )
        with contextlib.suppress(Exception):
            self.show_pending(rows)

    def _on_tool_call(self, data: dict) -> None:
        name = data.get("name", "?")
        args = data.get("args", {})
        self._close_prose()
        idx = self.ring.push("tool_call", name, tf.pretty_args(args))
        if name == "write_todos":
            self._print_todos(args, idx)
            return
        self._in_flight.append(name)
        if name in _SUBAGENT_TOOLS:
            self._print_subagent_call(name, args, idx)
        else:
            self._print_tool_call(name, args, idx)
        self._refresh()

    def _on_tool_result(self, data: dict) -> None:
        name = data.get("name", "tool")
        body = data.get("preview", "") or ""
        full = data.get("full", body) or body
        idx = self.ring.push("tool_result", name, full)
        with contextlib.suppress(ValueError):
            self._in_flight.remove(name)
        if not self._in_flight:
            self._sub_activity = ""
        if name == "write_todos":
            self._refresh()
            return
        is_err = tf.looks_like_error(full or body)
        if name == "task" and not is_err:
            self._print_subagent_response(full or body, idx)
        else:
            self._print_tool_result(name, full or body, idx, is_err)
        self._refresh()

    def _on_notice(self, data: dict) -> None:
        # Never silently dropped: a notice is what says "the model
        # returned nothing", and that must not look like a hang.
        text = data.get("text", "")
        if not text:
            return
        level = data.get("level", "info")
        style = {"error": "err", "warning": "warn"}.get(level, "chrome")
        self._close_prose()
        self._console.print()
        self._console.print(Text(f"{' ' * _MARKER_INDENT}{notice_mark(level)} {text}", style=style))
        if hint := action_hint(data):
            self._console.print(Text(f"{' ' * _PROSE_INDENT}{hint}", style="chrome"))

    # ------------------------------------------------------------------
    # Line printers (everything renders above the Live region)
    # ------------------------------------------------------------------

    def _print_tool_call(self, name: str, args: Any, idx: int) -> None:
        line = Text("  ")
        line.append(f"{gl.TOOL} ", style="tool")
        line.append(name, style="tool.bold")
        summary = tf.format_call(name, args)
        if summary:
            line.append(f"  {summary}")
        zone = self._zone_name(name, args)
        self._console.print()
        self._console.print(self._with_tail(line, zone, idx))
        if self._verbose and isinstance(args, dict) and args:
            pretty = tf.pretty_args(args)
            self._console.print(Padding(Text(pretty, style="chrome"), (0, 0, 0, 7)))

    def _with_tail(self, line: Text, zone: str, idx: int | str) -> Text:
        """Push the zone chip and the ring index out to the right margin.

        They are metadata about the call, not part of its name, and inline they
        sat between the reader and the argument they came to read. Right-aligned
        they form a column that can be ignored until it is wanted. A line
        already too long to fit just gets them appended — clipping the tail
        would lose the index `/expand` needs.
        """
        tail = Text()
        if zone:
            tail.append(f"{zone}   ", style="warn" if zone == "external" else "chrome")
        tail.append(f"#{idx}" if isinstance(idx, int) else idx, style="chrome")
        width = self._console.width or 80
        room = width - tail.cell_len - 2
        if line.cell_len > room:
            # Wrapping would put the index on a row of its own, orphaned from
            # the call it belongs to. The name is at the head of the line, so
            # what an ellipsis costs is the tail of an argument summary.
            line.truncate(max(1, room), overflow="ellipsis")
        line.append(" " * max(1, width - line.cell_len - tail.cell_len - 1))
        line.append_text(tail)
        return line

    def _print_tool_result(self, name: str, body: str, idx: int, is_err: bool) -> None:
        summary = tf.format_result(name, body, is_err)
        # No index here: the call line above carries it, and a result printed
        # straight under its own call is one statement, not two.
        line = Text(f"    {gl.RESULT} ", style="chrome")
        if is_err:
            line.append(f"{gl.FAIL} ", style="err")
            line.append(summary or "error", style="err")
            # The only place an index still earns its space: a failure is the
            # one result anybody goes looking for the whole body of.
            line.append(f"   /expand {idx}", style="chrome")
        else:
            line.append(f"{gl.OK} ", style="ok")
            line.append(summary or "ok", style="chrome")
        self._console.print(line)
        if self._verbose:
            pretty = tf.pretty_body(body)
            if len(pretty) > self.ring.entry_cap:
                pretty = pretty[: self.ring.entry_cap] + f"\n…[truncated; /expand {idx} for full]"
            self._console.print(Padding(Text(pretty), (0, 0, 0, 7)))

    def _print_subagent_call(self, name: str, args: Any, idx: int) -> None:
        desc = tf.format_call(name, args)
        line = Text("  ")
        line.append(f"{gl.SUBAGENT} ", style="subagent")
        line.append("subagent", style="subagent.bold")
        if desc:
            line.append(f"  {desc}")
        self._console.print()
        self._console.print(
            self._with_tail(line, "background" if name == "start_async_task" else "", idx)
        )

    def _print_subagent_response(self, body: str, idx: int) -> None:
        response = tf.extract_subagent_response(body)
        clipped = False
        if len(response) > _SUBAGENT_INLINE_CHARS:
            response = response[:_SUBAGENT_INLINE_CHARS]
            clipped = True
        header = Text(f"    {gl.RESULT} ", style="chrome")
        header.append(f"{gl.OK} subagent response", style="subagent")
        self._console.print(header)
        self._console.print()
        self._console.print(Padding(Markdown(response), (0, 2, 0, 7)))
        if clipped:
            self._console.print(
                Text(f"      …clipped — /expand {idx} for the full response", style="chrome")
            )

    def _print_artifact(self, data: dict) -> None:
        """Show the artifact inline, in a bounded box.

        Display-side only: the body is read from disk, never from the event,
        so nothing here re-enters the agent's context (see
        ``cli/render/artifact_view``). On any failure this degrades to the
        one-line announcement it replaced.
        """

        info = av.load_artifact(data)
        if info is not None:
            try:
                self._console.print()
                self._console.print(av.artifact_renderable(info))
                return
            except Exception:  # noqa: BLE001 — fall back to the plain line
                logger.debug("rendering artifact inline failed", exc_info=True)
        title, kind = av.artifact_summary(data)
        suffix = f" ({kind})" if kind else ""
        self._console.print(
            Text(f"  {gl.ARTIFACT} artifact: {tf.sanitize(title)}{suffix}", style="chrome")
        )

    def _print_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            self._console.print(Text(f"  {gl.NOTE} write_todos", style="chrome"))
            return
        self._console.print()
        header = Text("  ")
        header.append(f"{gl.PLAN} PLAN", style="tool.bold")
        self._console.print(self._with_tail(header, "", idx))
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = tf.sanitize(t.get("content", ""))
            # State is weight and glyph, never a new hue: done recedes, the one
            # in flight is the only bright thing, pending is plain text.
            if status == "completed":
                line = Text(f"    {gl.TODO_DONE}  {content}", style="chrome")
            elif status == "in_progress":
                line = Text(f"    {gl.TODO_RUNNING}  {content}", style="tool.bold")
            else:
                line = Text(f"    {gl.TODO_PENDING}  {content}")
            self._console.print(line)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _zone_name(self, name: str, args: Any) -> str:
        """Zone of the first path-ish arg ('' when not applicable)."""
        if not name.startswith("tr_"):
            return ""
        path = zones.path_argument(args)
        return zones.display_zone(path, self._workspace) if path else ""

    @staticmethod
    def _is_subagent_token(data: dict) -> bool:
        """Tokens streamed from inside the tools node / a subgraph.

        ``node``/``ns`` are added by ``astream_agent_iter`` from LangGraph's
        messages-mode metadata; when absent (older event shape) every token
        is treated as main-agent prose — same behavior as before.
        """
        ns = data.get("ns")
        if isinstance(ns, str) and ns:
            return True
        return data.get("node") == "tools"
