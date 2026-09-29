"""Plain ANSI renderer for the chat REPL (the non-rich fallback).

``ChatRenderer`` + ``RingEntry`` moved verbatim from ``chat_repl.py`` so
the rich renderer (``render/renderer.py``) can subclass without an
import cycle. Behavior is unchanged: terse ANSI lines to stderr, prose
to stdout through ``TokenSmoother``. The palette it draws with lives in
:mod:`yuyutsava.ansi`, which the daemon's terminal channel shares.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from yuyutsava.ansi import AGENT, BOLD, CHROME, DIM, ERR, OK, RESET, TOOL, WARN, YOU
from yuyutsava.cli.render import artifact_view as av
from yuyutsava.cli.render import glyphs as gl
from yuyutsava.cli.render import tool_format as tf
from yuyutsava.cli.render import zones
from yuyutsava.cli.render.notices import action_hint, notice_mark
from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.cli.stream_smoother import TokenSmoother, configured_cps, smoothing_enabled
from yuyutsava.core.streaming import StreamEvent

#: Cells the skills row may use for names. This renderer writes to a stream it
#: cannot measure — piped output has no width — so it assumes the conventional
#: eighty-column terminal rather than running off the edge of a narrow one.
_SKILLS_WIDTH = 60

#: The assistant's own marker, coloured once rather than at the call site.
AGENT_MARK = f"{AGENT}{BOLD}{gl.AGENT} yuyutsava:{RESET}"


def _err(line: str) -> None:
    """One line of transcript chrome, on stderr so piped stdout stays prose."""
    print(line, file=sys.stderr, flush=True)


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

    def user_message(self, text: str, *, when: str = "") -> None:
        """Print what you typed, so the transcript says who said what.

        The rich renderer's counterpart is the important one — this path is
        piped output and dumb terminals — but a piped transcript with only one
        side of the conversation in it is no more readable than a screen with
        only one side.
        """
        text = (text or "").strip()
        if not text:
            return
        stamp = when or _dt.datetime.now().strftime("%H:%M")
        _err("")
        _err(f"  {YOU}{BOLD}{gl.YOU} you:{RESET}  {CHROME}{stamp}{RESET}")
        for line in text.splitlines():
            _err(f"    {YOU}{line}{RESET}")

    def skills_recalled(self, names: tuple[str, ...]) -> None:
        """One chrome line naming the skills this turn matched."""
        if not names:
            return
        shown, rest = tf.fit_names(names, _SKILLS_WIDTH)
        tail = f"  +{rest} more" if rest else ""
        _err("")
        _err(f"  {CHROME}{gl.SKILLS} skills  {shown}{tail}{RESET}")

    def begin_turn(self) -> None:
        """Turn-start hook. The rich subclass starts its spinner here."""

    #: Repaint hook for a host that draws its own status bar. Nothing calls it
    #: on this renderer; it exists so the attribute is part of the base
    #: contract rather than something only the rich subclass happens to have.
    on_change: Any = None

    #: Likewise. This renderer prints a running log to a stream it cannot go
    #: back and rewrite, so it has no provisional rows to offer.
    show_pending: Any = None

    def status_text(self) -> str:
        """What the agent is doing, as plain text. Empty on this renderer.

        The plain renderer has no status line to keep — it prints a running
        log. Defined here so a host that asks for a status (the dashboard)
        degrades to "nothing to say" instead of raising.
        """
        return ""

    def status_state(self) -> str:
        """Always ``""``: this renderer prints a running log, not a status."""
        return ""

    def tool_in_flight(self) -> str:
        return ""

    def note_retry(
        self, model: str, attempt: int, retries: int, delay: float, exc: BaseException
    ) -> None:
        """Report a provider retry (429/503) in the renderer's own voice.

        Registered with ``llm.quirks.first_chunk_retry.add_retry_listener``.
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

        The rule itself lives in :func:`smoothing_enabled`, because the rich
        renderer needs exactly the same one and a second copy of it is how the
        two modes come to disagree about when text is paced.
        """
        if not smoothing_enabled():
            return None
        return TokenSmoother(
            sys.stdout.write,
            flush=sys.stdout.flush,
            base_cps=configured_cps(),
        )

    async def render(self, ev: StreamEvent) -> None:
        if ev.kind == "token":
            self._on_token(ev.data.get("text", ""))
            return

        # Any non-token event closes the AI stream visually. Flush the
        # smoother first so buffered prose lands before the tool/log line.
        if self._smoother is not None:
            await self._smoother.drain()
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

        data = ev.data
        match ev.kind:
            case "tool_call":
                self._on_tool_call(data)
            case "tool_result":
                self._on_tool_result(data)
            case "image":
                # A chart the agent generated must leave a line in a piped
                # transcript, or it reads exactly like one it never made. Named
                # rather than inlined — a pipe cannot show it.
                title = data.get("title") or data.get("path") or "image"
                _err(f"  {CHROME}{gl.ARTIFACT} visual: {tf.sanitize(title)}{RESET}")
            case "artifact":
                self._on_artifact(data)
            case "log":
                if text := data.get("text", ""):
                    _err(f"  {WARN}{text}{RESET}")
            case "notice":
                if text := data.get("text", ""):
                    level = data.get("level", "info")
                    colour = {"error": ERR, "warning": WARN}.get(level, CHROME)
                    _err(f"  {colour}{notice_mark(level)} {text}{RESET}")
                    if hint := action_hint(data):
                        _err(f"    {CHROME}{hint}{RESET}")
            case "final":
                # The token stream above already covered the prose. Just newline.
                print(flush=True)

    def _on_token(self, text: str) -> None:
        if not self._in_ai_stream:
            # Open the AI line with a small chip; no big separator block.
            print(f"\n  {AGENT_MARK}\n    ", end="", flush=True)
            self._in_ai_stream = True
        if self._smoother is not None:
            self._smoother.feed(text)
        else:
            print(text, end="", flush=True)

    def _on_tool_call(self, data: dict) -> None:
        name = data.get("name", "?")
        args = data.get("args", {})
        idx = self.ring.push("tool_call", name, tf.pretty_args(args))
        if name == "write_todos":
            self._render_todos(args, idx)
        elif self._verbose:
            self._render_tool_call_verbose(name, args, idx)
        else:
            self._render_tool_call_compact(name, args, idx)

    def _on_tool_result(self, data: dict) -> None:
        name = data.get("name", "tool")
        body = data.get("preview", "") or ""
        full = data.get("full", body) or body
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

    def _on_artifact(self, data: dict) -> None:
        """Name the artifact, and print its text when there is some.

        Read from disk and printed only — never fed back into the context.
        """
        info = av.load_artifact(data)
        if info is None:
            title, kind = av.artifact_summary(data)
            _err(f"  {CHROME}{gl.ARTIFACT} artifact: {title}{f' ({kind})' if kind else ''}{RESET}")
            return
        _err(f"  {CHROME}{gl.ARTIFACT} {info['title']} [{info['kind'] or info['mime']}]{RESET}")
        if info["text"]:
            _err(info["text"])
            if info["clipped"]:
                _err(f"  … clipped — full file: {info['path']}")
        elif info["path"]:
            _err(f"  open {info['path']}")

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
        summary = tf.compact_summary(args)
        zone_chip = self._zone_chip(name, args)
        head = f"  {TOOL}{gl.TOOL} {BOLD}{name}{RESET}"
        body = f"  {summary}" if summary else ""
        tail = f"{CHROME}  #{idx}{RESET}"
        print(f"\n{head}{body}{zone_chip}{tail}", file=sys.stderr, flush=True)

    def _render_tool_call_verbose(self, name: str, args: Any, idx: int) -> None:
        print(
            f"\n  {TOOL}{gl.TOOL} {BOLD}{name}{RESET}  {CHROME}#{idx}{RESET}",
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
                    sval = tf.truncate(sval, 200)
                print(f"    {DIM}{k}:{RESET} {sval}", file=sys.stderr)

    def _render_tool_result_verbose(self, name: str, body: str, idx: int, is_err: bool) -> None:
        status = f"{ERR}{gl.FAIL} error{RESET}" if is_err else f"{OK}{gl.OK} ok{RESET}"
        print(f"    {CHROME}{gl.RESULT}{RESET} {status}", file=sys.stderr)
        pretty = tf.pretty_body(body)
        if len(pretty) > self.ring.entry_cap:
            pretty = pretty[: self.ring.entry_cap] + f"\n…[truncated; /expand {idx} for full]"
        for line in pretty.splitlines():
            print(f"       {line}", file=sys.stderr)

    def _render_tool_result_compact_error(self, name: str, body: str, idx: int) -> None:
        short = tf.error_message(body) or "error"
        print(
            f"    {CHROME}{gl.RESULT}{RESET} {ERR}{gl.FAIL} {short}{RESET}"
            f"{CHROME}  /expand {idx}{RESET}",
            file=sys.stderr,
            flush=True,
        )

    def _render_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            print(f"  {CHROME}{gl.NOTE} write_todos{RESET}", file=sys.stderr, flush=True)
            return
        print(f"\n  {TOOL}{BOLD}{gl.PLAN} PLAN{RESET}  {CHROME}#{idx}{RESET}", file=sys.stderr)
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = str(t.get("content", "")).strip()
            symbol = self._todo_symbol(status)
            # State is weight, not a new hue: done recedes, the one in
            # flight is the only bright line, pending is plain.
            if status == "completed":
                colour = CHROME
            elif status == "in_progress":
                colour = f"{TOOL}{BOLD}"
            else:
                colour = ""
            reset = RESET if colour else ""
            print(f"    {colour}{symbol}  {content}{reset}", file=sys.stderr)

    @staticmethod
    def _todo_symbol(status: str) -> str:
        if status == "completed":
            return gl.TODO_DONE
        if status == "in_progress":
            return gl.TODO_RUNNING
        return gl.TODO_PENDING

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    def _zone_chip(self, name: str, args: Any) -> str:
        if not name.startswith("tr_"):
            return ""
        path = zones.path_argument(args)
        if not path:
            return ""
        zone = zones.display_zone(path, self._workspace)
        if not zone:
            return ""
        colour = WARN if zone == "external" else CHROME
        return f"  {colour}{zone}{RESET}"

    @staticmethod
    def _format_arg_value(v: Any) -> str:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.dumps(json.loads(stripped), indent=2, default=str)
                except ValueError:  # not JSON after all: show it as it came
                    pass
            return v
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, indent=2, default=str)
            except (TypeError, ValueError):
                return str(v)
        return str(v)
