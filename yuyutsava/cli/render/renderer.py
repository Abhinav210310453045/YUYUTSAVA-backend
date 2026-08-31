"""RichChatRenderer — Claude-Code-style transcript renderer.

Subclasses the plain :class:`~yuyutsava.cli.render.plain.ChatRenderer`
to reuse its ring buffer and slash-command helpers, overriding only the
display path:

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
from collections import deque
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.spinner import Spinner
from rich.text import Text

from yuyutsava.cli.render import tool_format as tf
from yuyutsava.cli.render.markdown_stream import MarkdownStream
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.core.streaming import StreamEvent

_SUBAGENT_TOOLS = ("task", "start_async_task")

# Cap for inline subagent-response rendering; /expand shows the rest.
_SUBAGENT_INLINE_CHARS = 4000


class RichChatRenderer(ChatRenderer):
    def __init__(
        self, *, verbose: bool, workspace: Path | None, console: Console
    ) -> None:
        super().__init__(verbose=verbose, workspace=workspace)
        # The Live region paces output; the smoother would double-buffer.
        self._smoother = None
        self._console = console
        self._md = MarkdownStream(console)
        self._live: Live | None = None
        self._in_flight: deque[str] = deque()
        self._opened_prose = False
        self._sub_activity = ""  # last line of nested subagent prose

    # ------------------------------------------------------------------
    # Turn / Live lifecycle
    # ------------------------------------------------------------------

    def begin_turn(self) -> None:
        """Start the spinner. Called by the REPL right before run_turn."""
        self._stop_live()
        self._opened_prose = False
        self._sub_activity = ""
        self._in_flight.clear()
        self._live = Live(
            self._status_renderable(),
            console=self._console,
            transient=True,
            refresh_per_second=10,
        )
        self._live.start()

    async def end_of_turn(self) -> None:
        self._md.finish()
        self._stop_live()
        self._in_flight.clear()
        self._sub_activity = ""

    @contextlib.contextmanager
    def pause(self):
        """Stop the Live region around blocking input (ask cards)."""
        had_live = self._live is not None
        self._stop_live()
        try:
            yield
        finally:
            if had_live:
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

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._status_renderable())

    def _status_renderable(self) -> RenderableType:
        if self._in_flight:
            name = self._in_flight[-1]
            if name in _SUBAGENT_TOOLS:
                label = Text.assemble(
                    ("Subagent working… ", "subagent"),
                    (f"({len(self._in_flight)} in flight)" if len(self._in_flight) > 1 else "", "chrome"),
                )
            else:
                label = Text.assemble(("Running ", "chrome"), (name, "tool"), ("…", "chrome"))
        elif self._md.started:
            label = Text("Writing…", style="chrome")
        else:
            label = Text("Thinking…", style="chrome")

        parts: list[RenderableType] = [Spinner("dots", text=label, style="accent")]
        tail = self._md.tail() if self._md.started else ""
        if tail:
            parts.append(Padding(Text(tail, style="chrome", no_wrap=False), (0, 0, 0, 2)))
        elif self._sub_activity:
            parts.append(
                Padding(Text(self._sub_activity, style="chrome"), (0, 0, 0, 2))
            )
        return Group(*parts)

    # ------------------------------------------------------------------
    # Event rendering
    # ------------------------------------------------------------------

    async def render(self, ev: StreamEvent) -> None:  # noqa: PLR0912
        if ev.kind == "token":
            text = ev.data.get("text", "")
            if not text:
                return
            if self._is_subagent_token(ev.data):
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
            return

        if ev.kind == "tool_call":
            name = ev.data.get("name", "?")
            args = ev.data.get("args", {})
            self._md.flush()
            idx = self._push_ring("tool_call", name, self._pretty(args))
            if name == "write_todos":
                self._print_todos(args, idx)
                return
            self._in_flight.append(name)
            if name in _SUBAGENT_TOOLS:
                self._print_subagent_call(name, args, idx)
            else:
                self._print_tool_call(name, args, idx)
            self._refresh()
            return

        if ev.kind == "tool_result":
            name = ev.data.get("name", "tool")
            body = ev.data.get("preview", "") or ""
            full = ev.data.get("full", body) or body
            idx = self._push_ring("tool_result", name, full)
            with contextlib.suppress(ValueError):
                self._in_flight.remove(name)
            if not self._in_flight:
                self._sub_activity = ""
            if name == "write_todos":
                self._refresh()
                return
            is_err = self._looks_like_error(full or body)
            if name == "task" and not is_err:
                self._print_subagent_response(full or body, idx)
            else:
                self._print_tool_result(name, full or body, idx, is_err)
            self._refresh()
            return

        if ev.kind == "log":
            text = ev.data.get("text", "")
            if text:
                self._console.print(Text(text, style="warn"))
            return

        if ev.kind == "image":
            title = ev.data.get("title") or ev.data.get("path") or "image"
            self._console.print(Text(f"  ◨ visual: {tf.sanitize(title)}", style="chrome"))
            return

        if ev.kind == "artifact":
            title = ev.data.get("title") or ev.data.get("id") or "artifact"
            self._console.print(Text(f"  ◨ artifact: {tf.sanitize(title)}", style="chrome"))
            return

        if ev.kind == "final":
            self._md.finish()
            return

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
            pretty = self._pretty(args)
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
            if len(pretty) > self._entry_cap:
                pretty = pretty[: self._entry_cap] + f"\n…[truncated; /expand {idx} for full]"
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
        if not (name.startswith("tr_") and isinstance(args, dict)):
            return ""
        path: str | None = None
        for k in ("path", "file_path", "target", "directory"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                path = v.strip()
                break
        if path is None:
            paths = args.get("paths")
            if isinstance(paths, list) and paths:
                p0 = paths[0]
                if isinstance(p0, str) and p0.strip():
                    path = p0.strip()
        if not path:
            return ""
        return self._classify_zone(path)

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
