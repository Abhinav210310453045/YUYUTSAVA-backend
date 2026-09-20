"""Plain ANSI renderer for the chat REPL (the non-rich fallback).

``ChatRenderer`` + ``RingEntry`` moved verbatim from ``chat_repl.py`` so
the rich renderer (``render/renderer.py``) can subclass without an
import cycle. Behavior is unchanged: terse ANSI lines to stderr, prose
to stdout through ``TokenSmoother``. The palette it draws with lives in
:mod:`yuyutsava.ansi`, which the daemon's terminal channel shares.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from yuyutsava.ansi import CYAN, DIM, GREEN, RED, RESET, YELLOW
from yuyutsava.cli.render import artifact_view as av
from yuyutsava.cli.render import tool_format as tf
from yuyutsava.cli.render import zones
from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.cli.stream_smoother import TokenSmoother
from yuyutsava.config.env import env_flag, env_float
from yuyutsava.core.streaming import StreamEvent


class ChatRenderer:
    """Print StreamEvents in a Claude-Code-style minimal layout.

    Two display modes share the same ring buffer and slash commands:

    * Non-verbose — one terse line per tool call (``· name: <summary>
      [zone] [#N]``). Successful tool results are suppressed; errors
      surface as a single red ``↳ name ✗ <msg>`` line.
    * Verbose — multi-line pretty-printed tool calls and results, JSON
      bodies indented two levels, truncation only at the per-entry cap.

    ``write_todos`` always renders as a checklist regardless of mode.
    Use ``/expand <n>`` to pull the full payload of any ``[#N]`` entry.
    """

    def __init__(self, *, verbose: bool, workspace: Path | None = None) -> None:
        self._verbose = verbose
        self._workspace = workspace
        self._in_ai_stream = False
        self.ring = PayloadRing()
        self._smoother = self._make_smoother()

    def begin_turn(self) -> None:
        """Turn-start hook. The rich subclass starts its spinner here."""

    #: Repaint hook for a host that draws its own status bar. Nothing calls it
    #: on this renderer; it exists so the attribute is part of the base
    #: contract rather than something only the rich subclass happens to have.
    on_change: Any = None

    def status_text(self) -> str:
        """What the agent is doing, as plain text. Empty on this renderer.

        The plain renderer has no status line to keep — it prints a running
        log. Defined here so a host that asks for a status (the dashboard)
        degrades to "nothing to say" instead of raising.
        """
        return ""

    def tool_in_flight(self) -> str:
        return ""

    def note_retry(
        self, model: str, attempt: int, retries: int, delay: float, exc: BaseException
    ) -> None:
        """Report a provider retry (429/503) in the renderer's own voice.

        Registered with ``llm.quirks.first_chunk_retry.set_retry_listener``.
        The provider's own retry logger writes several structlog lines per
        attempt, which in the rich renderer tore straight through the Live
        region and told the user nothing they could act on; here it is one
        line that names the wait.
        """
        print(
            f"  provider busy ({type(exc).__name__}) — retry {attempt}/{retries} in {delay:.0f}s",
            file=sys.stderr,
            flush=True,
        )

    @contextlib.contextmanager
    def pause(self):
        """No-op display pause. The rich subclass stops its Live region."""
        yield

    async def aclose(self) -> None:
        """Stop the token smoother, if prose was being paced through one."""
        if self._smoother is not None:
            await self._smoother.aclose()

    @staticmethod
    def _make_smoother() -> TokenSmoother | None:
        """Build a token smoother for prose, or None to print directly.

        Smoothing is for interactive terminals only: when stdout is not a TTY
        (piped output, tests) or the user disabled it via
        ``YUYUTSAVA_REPL_SMOOTH=0``, return None so the render path is
        byte-for-byte identical to printing chunks as they arrive.
        """
        if not env_flag("YUYUTSAVA_REPL_SMOOTH", default=True, false=("0", "false", "no", "off")):
            return None
        try:
            if not sys.stdout.isatty():
                return None
        except (ValueError, AttributeError):
            return None
        base_cps = env_float("YUYUTSAVA_REPL_SMOOTH_CPS", default=180.0)
        return TokenSmoother(
            sys.stdout.write,
            flush=sys.stdout.flush,
            base_cps=base_cps,
        )

    async def render(self, ev: StreamEvent) -> None:
        if ev.kind == "token":
            if not self._in_ai_stream:
                # Open the AI line with a small chip; no big separator block.
                print(f"\n{CYAN}🤖{RESET}  ", end="", flush=True)
                self._in_ai_stream = True
            text = ev.data.get("text", "")
            if self._smoother is not None:
                self._smoother.feed(text)
            else:
                print(text, end="", flush=True)
            return

        # Any non-token event closes the AI stream visually. Flush the
        # smoother first so buffered prose lands before the tool/log line.
        if self._smoother is not None:
            await self._smoother.drain()
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

        if ev.kind == "tool_call":
            name = ev.data.get("name", "?")
            args = ev.data.get("args", {})
            idx = self.ring.push("tool_call", name, tf.pretty_args(args))
            if name == "write_todos":
                self._render_todos(args, idx)
                return
            if self._verbose:
                self._render_tool_call_verbose(name, args, idx)
            else:
                self._render_tool_call_compact(name, args, idx)
            return

        if ev.kind == "tool_result":
            name = ev.data.get("name", "tool")
            body = ev.data.get("preview", "") or ""
            full = ev.data.get("full", body) or body
            idx = self.ring.push("tool_result", name, full)
            # The write_todos call line already reflects the new state.
            if name == "write_todos":
                return
            is_err = tf.looks_like_error(full or body)
            if self._verbose:
                self._render_tool_result_verbose(name, full or body, idx, is_err)
            elif is_err:
                self._render_tool_result_compact_error(name, full or body, idx)
            # else: suppress on success in non-verbose
            return

        if ev.kind == "image":
            # Dropped entirely here until now: a chart the agent generated left
            # no line in a piped transcript, which reads exactly like one it
            # never made. Named rather than inlined — a pipe cannot show it.
            title = ev.data.get("title") or ev.data.get("path") or "image"
            print(f"  ◨ visual: {tf.sanitize(title)}", file=sys.stderr, flush=True)
            return

        if ev.kind == "artifact":
            # Was dropped entirely here, so an artifact the agent built (a
            # table, a document) left no trace in a piped transcript. Read
            # from disk, printed only — never fed back into the context.
            info = av.load_artifact(ev.data)
            if info is None:
                title, kind = av.artifact_summary(ev.data)
                print(
                    f"  ◨ artifact: {title}{f' ({kind})' if kind else ''}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            head = f"  ◨ {info['title']} [{info['kind'] or info['mime']}]"
            print(head, file=sys.stderr, flush=True)
            if info["text"]:
                print(info["text"], file=sys.stderr, flush=True)
                if info["clipped"]:
                    print(f"  … clipped — full file: {info['path']}", file=sys.stderr, flush=True)
            elif info["path"]:
                print(f"  open {info['path']}", file=sys.stderr, flush=True)
            return

        if ev.kind == "log":
            text = ev.data.get("text", "")
            if text:
                print(f"{YELLOW}{text}{RESET}", file=sys.stderr, flush=True)
            return

        if ev.kind == "notice":
            text = ev.data.get("text", "")
            if text:
                level = ev.data.get("level", "info")
                colour = {"error": RED, "warning": YELLOW}.get(level, DIM)
                mark = {"error": "✗", "warning": "⚠"}.get(level, "·")
                print(f"  {colour}{mark} {text}{RESET}", file=sys.stderr, flush=True)
            return

        if ev.kind == "final":
            # The token stream above already covered the prose. Just newline.
            print(flush=True)

    async def end_of_turn(self) -> None:
        """Force-close any dangling streaming line."""
        if self._smoother is not None:
            await self._smoother.drain()
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

    # ------------------------------------------------------------------
    # Ring buffer + slash command helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Mode-specific renderers
    # ------------------------------------------------------------------

    def _render_tool_call_compact(self, name: str, args: Any, idx: int) -> None:
        summary = self._compact_summary(args)
        zone_chip = self._zone_chip(name, args)
        head = f"  {DIM}·{RESET} {CYAN}{name}{RESET}"
        body = f"{DIM}: {summary}{RESET}" if summary else ""
        tail = f"{DIM}  [#{idx}]{RESET}"
        print(f"{head}{body}{zone_chip}{tail}", file=sys.stderr, flush=True)

    def _render_tool_call_verbose(self, name: str, args: Any, idx: int) -> None:
        print(
            f"\n  {DIM}·{RESET} {CYAN}{name}{RESET}  {DIM}[#{idx}]{RESET}",
            file=sys.stderr,
        )
        if not isinstance(args, dict) or not args:
            return
        for k, v in args.items():
            sval = self._format_arg_value(v)
            if "\n" in sval:
                print(f"    {DIM}{k}:{RESET}", file=sys.stderr)
                for line in sval.splitlines():
                    print(f"      {line}", file=sys.stderr)
            else:
                if len(sval) > 200:
                    sval = sval[:200] + "…"
                print(f"    {DIM}{k}:{RESET} {sval}", file=sys.stderr)

    def _render_tool_result_verbose(self, name: str, body: str, idx: int, is_err: bool) -> None:
        status = f"{RED}✗ error{RESET}" if is_err else f"{GREEN}← ok{RESET}"
        print(
            f"  {DIM}↳{RESET} {CYAN}{name}{RESET} {status}  {DIM}[#{idx}]{RESET}",
            file=sys.stderr,
        )
        pretty = self._pretty_body(body)
        if len(pretty) > self.ring.entry_cap:
            pretty = pretty[: self.ring.entry_cap] + f"\n…[truncated; /expand {idx} for full]"
        for line in pretty.splitlines():
            print(f"      {line}", file=sys.stderr)

    def _render_tool_result_compact_error(self, name: str, body: str, idx: int) -> None:
        short = tf.error_message(body)
        if short:
            tail = f"{DIM}  [#{idx}]{RESET}"
            print(
                f"  {RED}↳ {name} ✗ {short}{RESET}{tail}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"  {RED}↳ {name} ✗ error{RESET}{DIM}  [#{idx}]{RESET}",
                file=sys.stderr,
                flush=True,
            )

    def _render_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            print(
                f"  {DIM}· write_todos  [#{idx}]{RESET}",
                file=sys.stderr,
                flush=True,
            )
            return
        print(
            f"\n  {CYAN}TODO:{RESET}  {DIM}[#{idx}]{RESET}",
            file=sys.stderr,
        )
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = str(t.get("content", "")).strip()
            symbol = self._todo_symbol(status)
            if status == "completed":
                colour = DIM
            elif status == "in_progress":
                colour = GREEN
            else:
                colour = ""
            reset = RESET if colour else ""
            print(
                f"    {colour}{symbol} {content}{reset}",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    @staticmethod
    def _todo_symbol(status: str) -> str:
        if status == "completed":
            return "[✓]"
        if status == "in_progress":
            return "[▶]"
        return "[ ]"

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compact_summary(args: Any) -> str:
        if not isinstance(args, dict):
            s = str(args)
            return s if len(s) <= 80 else s[:80] + "…"
        reason = args.get("reason")
        if isinstance(reason, str) and reason.strip():
            s = reason.strip()
            return s if len(s) <= 80 else s[:80] + "…"
        for key in ("path", "file_path", "target", "directory"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        paths = args.get("paths")
        if isinstance(paths, list) and paths:
            head = ", ".join(str(p) for p in paths[:2])
            return head + ("…" if len(paths) > 2 else "")
        for key in ("query", "pattern", "name", "command", "question"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if len(s) > 80:
                    s = s[:80] + "…"
                return f'{key}="{s}"'
        # Fallback: terse key=value summary.
        items: list[str] = []
        for k, v in args.items():
            sval = str(v)
            if len(sval) > 40:
                sval = sval[:40] + "…"
            items.append(f"{k}={sval}")
        out = ", ".join(items)
        return out if len(out) <= 80 else out[:80] + "…"

    def _zone_chip(self, name: str, args: Any) -> str:
        if not name.startswith("tr_"):
            return ""
        path = zones.path_argument(args)
        if not path:
            return ""
        zone = zones.display_zone(path, self._workspace)
        if not zone:
            return ""
        colour = YELLOW if zone == "external" else DIM
        return f"  {colour}[{zone}]{RESET}"

    @staticmethod
    def _format_arg_value(v: Any) -> str:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.dumps(json.loads(stripped), indent=2, default=str)
                except Exception:
                    pass
            return v
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, indent=2, default=str)
            except Exception:
                return str(v)
        return str(v)

    @staticmethod
    def _pretty_body(body: str) -> str:
        if not isinstance(body, str):
            return str(body)
        stripped = body.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.dumps(json.loads(stripped), indent=2, default=str)
            except Exception:
                pass
        return body
