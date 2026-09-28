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
import logging
from collections import deque
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.spinner import Spinner
from rich.text import Text

from yuyutsava.cli.render import artifact_view as av
from yuyutsava.cli.render import tool_format as tf
from yuyutsava.cli.render import zones
from yuyutsava.cli.render.markdown_stream import MarkdownStream
from yuyutsava.cli.render.notices import action_hint, notice_mark
from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.llm.quirks.first_chunk_retry import classify_provider_busy

logger = logging.getLogger("yuyutsava.cli.render.renderer")

_SUBAGENT_TOOLS = ("task", "start_async_task")

# Cap for inline subagent-response rendering; /expand shows the rest.
_SUBAGENT_INLINE_CHARS = 4000


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
        self._md = MarkdownStream(console)
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
        self._md.finish()
        self._stop_live()
        self._in_flight.clear()
        self._sub_activity = ""
        self._retry_note = ""
        self._in_turn = False
        self._notify()

    async def aclose(self) -> None:
        """Nothing to release: the Live region paces output itself, so this
        renderer never builds a token smoother to begin with."""

    @contextlib.contextmanager
    def pause(self):
        """Stop the Live region around blocking input (ask cards).

        A no-op under the dashboard: ask cards render into the transcript pane,
        so there is no repainting region to get out of the way of.
        """
        had_live = self._live is not None
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

    def tool_in_flight(self) -> str:
        """Name of the most recent unfinished tool call, or ``""``."""
        return self._in_flight[-1] if self._in_flight else ""

    def _status_renderable(self) -> RenderableType:
        state, detail = self._activity()
        if state == "retry":
            return Group(Spinner("dots", text=Text(detail, style="warn"), style="warn"))
        if state == "subagent":
            label = Text.assemble(("Subagent working… ", "subagent"), (detail, "chrome"))
        elif state == "tool":
            label = Text.assemble(("Running ", "chrome"), (detail, "tool"), ("…", "chrome"))
        elif state == "writing":
            label = Text("Writing…", style="chrome")
        else:
            label = Text("Thinking…", style="chrome")

        parts: list[RenderableType] = [Spinner("dots", text=label, style="accent")]
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
                self._console.print(Text(f"  ◨ visual: {tf.sanitize(title)}", style="chrome"))
            case "artifact":
                self._print_artifact(data)
            case "final":
                self._md.finish()

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
            self._console.print("🤖", style="accent")
            self._opened_prose = True
        self._md.feed(text)
        self._refresh()

    def _on_tool_call(self, data: dict) -> None:
        name = data.get("name", "?")
        args = data.get("args", {})
        self._md.flush()
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
        self._md.flush()
        self._console.print()
        self._console.print(Text(f"  {notice_mark(level)} {text}", style=style))
        if hint := action_hint(data):
            self._console.print(Text(f"    {hint}", style="chrome"))

    # ------------------------------------------------------------------
    # Line printers (everything renders above the Live region)
    # ------------------------------------------------------------------

    def _print_tool_call(self, name: str, args: Any, idx: int) -> None:
        line = Text("  ")
        line.append("● ", style="accent")
        line.append(name, style="tool")
        summary = tf.format_call(name, args)
        if summary:
            line.append(f"({summary})", style="default")
        zone = self._zone_name(name, args)
        if zone:
            line.append(f"  [{zone}]", style="warn" if zone == "external" else "chrome")
        line.append(f"  [#{idx}]", style="chrome")
        self._console.print(line)
        if self._verbose and isinstance(args, dict) and args:
            pretty = tf.pretty_args(args)
            self._console.print(Padding(Text(pretty, style="chrome"), (0, 0, 0, 6)))

    def _print_tool_result(self, name: str, body: str, idx: int, is_err: bool) -> None:
        summary = tf.format_result(name, body, is_err)
        line = Text("    ⎿ ", style="chrome")
        if is_err:
            line.append("✗ ", style="err")
            line.append(summary or "error", style="err")
        else:
            line.append("✓ ", style="ok")
            line.append(summary or "ok", style="chrome")
        line.append(f"  [#{idx}]", style="chrome")
        self._console.print(line)
        if self._verbose:
            pretty = tf.pretty_body(body)
            if len(pretty) > self.ring.entry_cap:
                pretty = pretty[: self.ring.entry_cap] + f"\n…[truncated; /expand {idx} for full]"
            self._console.print(Padding(Text(pretty), (0, 0, 0, 6)))

    def _print_subagent_call(self, name: str, args: Any, idx: int) -> None:
        desc = tf.format_call(name, args)
        line = Text("  ")
        line.append("◆ ", style="subagent")
        line.append("Subagent", style="subagent")
        if desc:
            line.append(f"({desc})", style="default")
        if name == "start_async_task":
            line.append("  [background]", style="chrome")
        line.append(f"  [#{idx}]", style="chrome")
        self._console.print(line)

    def _print_subagent_response(self, body: str, idx: int) -> None:
        response = tf.extract_subagent_response(body)
        clipped = False
        if len(response) > _SUBAGENT_INLINE_CHARS:
            response = response[:_SUBAGENT_INLINE_CHARS]
            clipped = True
        header = Text("    ⎿ ", style="chrome")
        header.append("✓ subagent response", style="subagent")
        header.append(f"  [#{idx}]", style="chrome")
        self._console.print(header)
        self._console.print(Padding(Markdown(response), (0, 2, 0, 6)))
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
        self._console.print(Text(f"  ◨ artifact: {tf.sanitize(title)}{suffix}", style="chrome"))

    def _print_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            self._console.print(Text(f"  · write_todos  [#{idx}]", style="chrome"))
            return
        self._console.print()
        header = Text("  TODO", style="accent")
        header.append(f"  [#{idx}]", style="chrome")
        self._console.print(header)
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = tf.sanitize(t.get("content", ""))
            if status == "completed":
                line = Text(f"    ✓ {content}", style="chrome strike")
            elif status == "in_progress":
                line = Text(f"    ◐ {content}", style="bold ok")
            else:
                line = Text(f"    ☐ {content}")
            self._console.print(line)
        self._console.print()

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
