"""``yuyutsava chat`` — interactive multi-turn terminal REPL.

The one-shot ``yuyutsava "<task>"`` flow in ``cli/commands/chat.py`` builds
the agent stack, runs one turn, and exits. This module keeps the agent
stack alive across many turns under a single session/thread_id, renders a
clean Claude-Code-style chat UI, and tears everything down gracefully on
Ctrl+D or ``/quit``.

Design:
  * Bypasses the noisy print path inside ``astream_agent`` (which prints
    its own '🤖 AI (streaming)' separators) by consuming the structured
    ``astream_agent_iter`` events instead.
  * Silences third-party loggers (langgraph_api, httpx, langfuse, …) and
    redirects fd 1/2 around the agent-stack build so the LangGraph host's
    startup banner stays off-screen.
  * Persists every turn through the same ``SessionStore`` the one-shot
    flow uses — sessions show up identically in ``--list-sessions`` and
    in the Electron UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from yuyutsava.cli.agent_stack import build_cli_agent_stack
from yuyutsava.cli.stream_smoother import TokenSmoother
from yuyutsava.consent import decision_token as _decision_token
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.engine import cleanup_local_sandbox, silence_plumbing_loggers
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.storage.paths import state_dir
from yuyutsava.storage.sessions import (
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

_BANNER_LINES = [
    "╦ ╦ ╦ ╦ ╦ ╦ ╦ ╦ ╔╦╗ ╔═╗ ╔═╗ ╦  ╦ ╔═╗",
    "╚╦╝ ║ ║ ╚╦╝ ║ ║  ║  ╚═╗ ╠═╣ ╚╗╔╝ ╠═╣",
    " ╩  ╚═╝  ╩  ╚═╝  ╩  ╚═╝ ╩ ╩  ╚╝  ╩ ╩",
]

_CYAN = "\033[36m"
_DIM = "\033[2m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _print_version_notice() -> None:
    """Render langgraph-api upgrade/support notices once, above the banner.

    langgraph_api normally emits these from a background daemon thread that
    lands mid-chat (disabled via ``LANGGRAPH_NO_VERSION_CHECK`` in
    ``AsyncSubagentHost.start``). Here we run the *same* check synchronously so
    any notice appears cleanly before the YUYUTSAVA graphic instead of
    interleaving with the conversation. Best-effort — never breaks startup.
    """
    import logging as _logging

    log = _logging.getLogger("version_check")
    lines: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _Capture()
    saved = (log.level, log.propagate, log.disabled, list(log.handlers))
    saved_env = os.environ.get("LANGGRAPH_NO_VERSION_CHECK")
    try:
        from langgraph_api import __version__ as _lg_version
        from langgraph_api.cli import _check_newer_version

        # The check early-returns when this is set, and attaches its own stderr
        # handler when the logger has none — clear the flag and pre-install our
        # capture handler (propagate off) so it neither skips nor prints itself.
        os.environ["LANGGRAPH_NO_VERSION_CHECK"] = ""
        log.handlers = [handler]
        log.propagate = False
        log.disabled = False
        log.setLevel(_logging.INFO)
        _check_newer_version("langgraph-api", _lg_version)
    except Exception:
        return
    finally:
        log.setLevel(saved[0])
        log.propagate = saved[1]
        log.disabled = saved[2]
        log.handlers = saved[3]
        if saved_env is None:
            os.environ.pop("LANGGRAPH_NO_VERSION_CHECK", None)
        else:
            os.environ["LANGGRAPH_NO_VERSION_CHECK"] = saved_env

    if not lines:
        return
    for msg in lines:
        for line in msg.splitlines():
            print(f"{_DIM}{_YELLOW}{line}{_RESET}", file=sys.stderr)


def _print_banner(*, session_id: str, workspace: Path, resuming: bool) -> None:
    print(file=sys.stderr)
    for line in _BANNER_LINES:
        print(f"{_CYAN}{line}{_RESET}", file=sys.stderr)
    verb = "resumed session" if resuming else "session"
    print(file=sys.stderr)
    print(f"  {_DIM}{verb}:{_RESET}  {session_id}", file=sys.stderr)
    print(f"  {_DIM}workspace:{_RESET} {workspace}", file=sys.stderr)
    print(
        f"  {_DIM}type /help, /quit, or press Ctrl+D to exit{_RESET}",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def _print_help() -> None:
    print(file=sys.stderr)
    print(f"{_CYAN}Commands:{_RESET}", file=sys.stderr)
    print(f"  {_DIM}/help{_RESET}         show this help", file=sys.stderr)
    print(f"  {_DIM}/quit{_RESET}         exit the chat (Ctrl+D also works)", file=sys.stderr)
    print(f"  {_DIM}/clear{_RESET}        redraw the banner", file=sys.stderr)
    print(f"  {_DIM}/new{_RESET}          start a fresh session in this process", file=sys.stderr)
    print(f"  {_DIM}/session{_RESET}      show the current session id", file=sys.stderr)
    print(f"  {_DIM}/ring{_RESET}         list recent tool calls/results with their [#n] indices", file=sys.stderr)
    print(f"  {_DIM}/last [k]{_RESET}     print the last k captured payloads in full (default 1)", file=sys.stderr)
    print(f"  {_DIM}/expand <n>{_RESET}   print the full body of the [#n] entry", file=sys.stderr)
    print(file=sys.stderr)
    print(f"{_DIM}Ctrl+C cancels the current turn but keeps the session open.{_RESET}", file=sys.stderr)
    print(file=sys.stderr)


# ---------------------------------------------------------------------------
# Log silencing
# ---------------------------------------------------------------------------
#
# The plumbing-logger floor + the warnings filter live in
# ``yuyutsava.core.engine.silence_plumbing_loggers`` so the same rules apply
# to the daemon. The REPL just calls it.


@contextlib.contextmanager
def _suppress_stdio():
    """Temporarily route fd 1 / fd 2 to /dev/null.

    Used around ``build_cli_agent_stack()`` to catch the LangGraph server
    banner — that text is written from a daemon thread spawned by
    ``langgraph_api.cli.run_server`` and bypasses Python's logging, so
    fd-level redirection is the only reliable mute.
    """
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(devnull_fd)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)


# ---------------------------------------------------------------------------
# Renderer for StreamEvent
# ---------------------------------------------------------------------------


@dataclass
class RingEntry:
    """One captured event with its full untruncated payload.

    Stored in ``ChatRenderer._ring`` so ``/last`` and ``/expand <n>`` can
    surface payloads the truncated preview hid. Per-entry body is capped
    by ``YUYUTSAVA_REPL_RING_ENTRY_KB`` (default 100 KB) to keep huge tool
    outputs from blowing up REPL memory.
    """
    index: int
    kind: str           # "tool_call" | "tool_result"
    name: str           # tool name
    full: str           # pretty-printed full body (already capped)
    truncated: bool     # True if the body was clipped to the per-entry cap


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
        try:
            ring_size = max(1, int(os.environ.get("YUYUTSAVA_REPL_RING", "50")))
        except ValueError:
            ring_size = 50
        try:
            self._entry_cap = max(1024, int(os.environ.get("YUYUTSAVA_REPL_RING_ENTRY_KB", "100")) * 1024)
        except ValueError:
            self._entry_cap = 100 * 1024
        self._ring: deque[RingEntry] = deque(maxlen=ring_size)
        self._next_index = 1
        self._smoother = self._make_smoother()

    @staticmethod
    def _make_smoother() -> TokenSmoother | None:
        """Build a token smoother for prose, or None to print directly.

        Smoothing is for interactive terminals only: when stdout is not a TTY
        (piped output, tests) or the user disabled it via
        ``YUYUTSAVA_REPL_SMOOTH=0``, return None so the render path is
        byte-for-byte identical to printing chunks as they arrive.
        """
        flag = os.environ.get("YUYUTSAVA_REPL_SMOOTH", "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return None
        try:
            if not sys.stdout.isatty():
                return None
        except (ValueError, AttributeError):
            return None
        try:
            base_cps = float(os.environ.get("YUYUTSAVA_REPL_SMOOTH_CPS", "180"))
        except ValueError:
            base_cps = 180.0
        return TokenSmoother(
            sys.stdout.write,
            flush=sys.stdout.flush,
            base_cps=base_cps,
        )

    async def render(self, ev: StreamEvent) -> None:
        if ev.kind == "token":
            if not self._in_ai_stream:
                # Open the AI line with a small chip; no big separator block.
                print(f"\n{_CYAN}🤖{_RESET}  ", end="", flush=True)
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
            idx = self._push_ring("tool_call", name, self._pretty(args))
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
            idx = self._push_ring("tool_result", name, full)
            # The write_todos call line already reflects the new state.
            if name == "write_todos":
                return
            is_err = self._looks_like_error(full or body)
            if self._verbose:
                self._render_tool_result_verbose(name, full or body, idx, is_err)
            elif is_err:
                self._render_tool_result_compact_error(name, full or body, idx)
            # else: suppress on success in non-verbose
            return

        if ev.kind == "log":
            text = ev.data.get("text", "")
            if text:
                print(f"{_YELLOW}{text}{_RESET}", file=sys.stderr, flush=True)
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

    def _push_ring(self, kind: str, name: str, full: str) -> int:
        truncated = False
        if len(full) > self._entry_cap:
            full = full[: self._entry_cap] + f"\n…[truncated to {self._entry_cap // 1024}KB]"
            truncated = True
        idx = self._next_index
        self._next_index += 1
        self._ring.append(RingEntry(index=idx, kind=kind, name=name, full=full, truncated=truncated))
        return idx

    def _find(self, idx: int) -> RingEntry | None:
        for e in self._ring:
            if e.index == idx:
                return e
        return None

    def print_ring(self) -> None:
        if not self._ring:
            print(f"{_DIM}(ring empty){_RESET}", file=sys.stderr)
            return
        print(f"{_CYAN}ring (most recent last):{_RESET}", file=sys.stderr)
        for e in self._ring:
            summary = e.full.replace("\n", " ⏎ ")
            if len(summary) > 100:
                summary = summary[:100] + "…"
            tag = "·" if e.kind == "tool_call" else "↳"
            print(f"  {_DIM}[#{e.index}] {tag} {e.name}: {summary}{_RESET}", file=sys.stderr)

    def print_entry(self, idx: int) -> None:
        e = self._find(idx)
        if e is None:
            print(f"{_DIM}no ring entry #{idx} (try /ring){_RESET}", file=sys.stderr)
            return
        header = f"[#{e.index}] {e.kind} {e.name}"
        if e.truncated:
            header += " (truncated)"
        print(f"{_CYAN}{header}{_RESET}", file=sys.stderr)
        print(e.full, file=sys.stderr)

    def print_last(self, k: int = 1) -> None:
        if not self._ring:
            print(f"{_DIM}(ring empty){_RESET}", file=sys.stderr)
            return
        k = max(1, k)
        for e in list(self._ring)[-k:]:
            self.print_entry(e.index)

    @staticmethod
    def _pretty(args: Any) -> str:
        try:
            if isinstance(args, (dict, list)):
                return json.dumps(args, indent=2, default=str)
        except Exception:
            pass
        return str(args)

    @staticmethod
    def _fmt_args(args: Any, *, limit: int) -> str:
        try:
            if isinstance(args, dict):
                items = []
                for k, v in args.items():
                    sval = str(v)
                    if len(sval) > 40:
                        sval = sval[:40] + "…"
                    items.append(f"{k}={sval}")
                out = ", ".join(items)
            else:
                out = str(args)
        except Exception:
            out = "…"
        if len(out) > limit:
            out = out[:limit] + "…"
        return out

    # ------------------------------------------------------------------
    # Mode-specific renderers
    # ------------------------------------------------------------------

    def _render_tool_call_compact(self, name: str, args: Any, idx: int) -> None:
        summary = self._compact_summary(args)
        zone_chip = self._zone_chip(name, args)
        head = f"  {_DIM}·{_RESET} {_CYAN}{name}{_RESET}"
        body = f"{_DIM}: {summary}{_RESET}" if summary else ""
        tail = f"{_DIM}  [#{idx}]{_RESET}"
        print(f"{head}{body}{zone_chip}{tail}", file=sys.stderr, flush=True)

    def _render_tool_call_verbose(self, name: str, args: Any, idx: int) -> None:
        print(
            f"\n  {_DIM}·{_RESET} {_CYAN}{name}{_RESET}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        if not isinstance(args, dict) or not args:
            return
        for k, v in args.items():
            sval = self._format_arg_value(v)
            if "\n" in sval:
                print(f"    {_DIM}{k}:{_RESET}", file=sys.stderr)
                for line in sval.splitlines():
                    print(f"      {line}", file=sys.stderr)
            else:
                if len(sval) > 200:
                    sval = sval[:200] + "…"
                print(f"    {_DIM}{k}:{_RESET} {sval}", file=sys.stderr)

    def _render_tool_result_verbose(
        self, name: str, body: str, idx: int, is_err: bool
    ) -> None:
        status = f"{_RED}✗ error{_RESET}" if is_err else f"{_GREEN}← ok{_RESET}"
        print(
            f"  {_DIM}↳{_RESET} {_CYAN}{name}{_RESET} {status}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        pretty = self._pretty_body(body)
        if len(pretty) > self._entry_cap:
            pretty = (
                pretty[: self._entry_cap]
                + f"\n…[truncated; /expand {idx} for full]"
            )
        for line in pretty.splitlines():
            print(f"      {line}", file=sys.stderr)

    def _render_tool_result_compact_error(
        self, name: str, body: str, idx: int
    ) -> None:
        short = self._error_message(body)
        if short:
            tail = f"{_DIM}  [#{idx}]{_RESET}"
            print(
                f"  {_RED}↳ {name} ✗ {short}{_RESET}{tail}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"  {_RED}↳ {name} ✗ error{_RESET}{_DIM}  [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )

    def _render_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            print(
                f"  {_DIM}· write_todos  [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )
            return
        print(
            f"\n  {_CYAN}TODO:{_RESET}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = str(t.get("content", "")).strip()
            symbol = self._todo_symbol(status)
            if status == "completed":
                colour = _DIM
            elif status == "in_progress":
                colour = _GREEN
            else:
                colour = ""
            reset = _RESET if colour else ""
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
        zone = self._classify_zone(path)
        if not zone:
            return ""
        colour = _YELLOW if zone == "external" else _DIM
        return f"  {colour}[{zone}]{_RESET}"

    def _classify_zone(self, path: str) -> str:
        if self._workspace is not None and path.startswith(str(self._workspace)):
            return "workspace"
        if path.startswith(("/tmp", "/var/folders", "/private/tmp")):
            return "sandbox"
        if path.startswith("/"):
            return "external"
        return ""

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

    @staticmethod
    def _looks_like_error(body: str) -> bool:
        if not isinstance(body, str) or not body:
            return False
        stripped = body.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    status = str(obj.get("status", "")).lower()
                    if status in ("error", "rejected", "fail", "failed"):
                        return True
                    err = obj.get("error")
                    if err:
                        return True
            except Exception:
                pass
        head = stripped[:200].lower()
        markers = ("error:", "exception:", "traceback", "failed:", "✗")
        return any(m in head for m in markers)

    @staticmethod
    def _error_message(body: str) -> str:
        if not isinstance(body, str):
            return ""
        stripped = body.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    err = obj.get("error")
                    if isinstance(err, dict):
                        msg = err.get("message") or err.get("code")
                        if msg:
                            return str(msg)[:120]
                    elif isinstance(err, str) and err:
                        return err[:120]
                    msg = obj.get("message")
                    if isinstance(msg, str) and msg:
                        return msg[:120]
            except Exception:
                pass
        first = stripped.splitlines()[0] if stripped else ""
        return first[:120]


# ---------------------------------------------------------------------------
# Ask handler — bridges astream_agent_iter interrupts to a stdin prompt
# ---------------------------------------------------------------------------


def _print_kv(label: str, value: str) -> None:
    print(f"  {_DIM}{label:<10}{_RESET} {value}", file=sys.stderr)


def _render_permission_payload(payload: dict[str, Any]) -> None:
    """Render the typed interrupt body so the user knows what's being asked.

    Mirrors the field layout used by ``prompt_permission`` (non-chat CLI)
    and the Electron card builder in ``daemon/orchestrator_loop`` so the
    three surfaces stay consistent.
    """
    itype = payload.get("type", "")

    if itype == "task_runner_permission":
        op = str(payload.get("operation") or "?").upper()
        paths = payload.get("paths") or []
        path_str = ", ".join(paths) if isinstance(paths, list) else str(paths)
        zone = str(payload.get("zone") or "?").upper()
        reason = payload.get("reason") or ""
        risk = payload.get("risk_level") or ""
        agent = payload.get("requesting_agent") or ""
        parent = payload.get("parent_agent") or ""

        _print_kv("Operation", op)
        if path_str:
            _print_kv("Path(s)", path_str)
        _print_kv("Zone", zone)
        if agent:
            line = agent + (f"  (parent: {parent})" if parent else "")
            _print_kv("Agent", line)
        if reason:
            _print_kv("Reason", reason)
        if risk:
            _print_kv("Risk", str(risk).upper())
        return

    if itype == "permission_request":
        command = payload.get("command") or ""
        reason = payload.get("reason") or ""
        if command:
            _print_kv("Command", command)
        if reason:
            _print_kv("Reason", reason)
        return

    # Unknown / loose payload — best-effort body so something is visible.
    body = payload.get("body") or payload.get("command") or payload.get("reason") or ""
    if body:
        print(f"  {body}", file=sys.stderr)


async def _ask_handler(interrupt_value: Any) -> str:
    """Render a permission/question interrupt and read the user's reply."""
    payload = interrupt_value if isinstance(interrupt_value, dict) else {"text": str(interrupt_value)}
    itype = payload.get("type", "")

    print(file=sys.stderr)
    if itype == "user_question":
        title = payload.get("title") or "Question"
        body = payload.get("body") or payload.get("question") or ""
        print(f"{_YELLOW}? {title}{_RESET}", file=sys.stderr)
        if body:
            print(f"  {body}", file=sys.stderr)
        prompt_text = "> "
        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(None, lambda: input(prompt_text).strip())
        except (EOFError, KeyboardInterrupt):
            return "reject"
        return answer or "no response"

    if itype == "task_runner_permission":
        op = str(payload.get("operation") or "?").upper()
        title = f"Permission requested — {op}"
    elif itype == "permission_request":
        title = "Permission requested — execute"
    else:
        title = payload.get("title") or "Permission requested"

    print(f"{_YELLOW}▣ {title}{_RESET}", file=sys.stderr)
    _render_permission_payload(payload)
    # Offer the allowlist scopes for every task-runner operation type (matches the
    # daemon's options_for_interrupt). Choosing [s]ession / [p]roject remembers the
    # op for the whole workspace so it isn't re-asked per file/subfolder.
    if itype == "task_runner_permission":
        hint = "[y]es / [n]o / [s]ession / [p]roject"
    else:
        hint = "[y]es / [n]o  (also: approve / reject)"
    print(f"  {_DIM}{hint}{_RESET}", file=sys.stderr)

    loop = asyncio.get_running_loop()
    # Re-prompt on a blank / unrecognized line instead of silently rejecting:
    # with several parallel asks under prompt_toolkit, a stray buffered line could
    # otherwise be misread as a rejection. Explicit reject words and EOF/Ctrl-C
    # still reject; we cap retries so a closed stdin can't spin forever.
    for _ in range(3):
        try:
            raw = await loop.run_in_executor(None, lambda: input("approve/reject> ").strip())
        except (EOFError, KeyboardInterrupt):
            return "reject"
        if not raw:
            continue
        token = _decision_token(raw)
        if token is not None:
            return token
        print(f"  {_DIM}please answer: [y]es / [n]o / [s]ession / [p]roject{_RESET}", file=sys.stderr)
    return "reject"


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


_SLASH_QUIT = object()
_SLASH_HANDLED = object()


def _loopback_url(url: str) -> str:
    """Rewrite a daemon web URL to loopback so the CLI is auth-exempt.

    The daemon may advertise a non-loopback bind (e.g. ``http://0.0.0.0:7654``)
    with bearer auth enforced; connecting from 127.0.0.1 is exempt.
    """
    return url.replace("://0.0.0.0", "://127.0.0.1").replace("://[::]", "://127.0.0.1")


async def _handle_ask_command(cmd: str, remote: Any) -> bool:
    """Handle background-approval slash commands against the daemon (async).

    Returns True if *cmd* was an ask command (and was handled), else False so
    the caller falls through to the normal slash handler / agent turn.
    """
    c = cmd.strip()
    if not c.startswith("/"):
        return False
    parts = c.split()
    head = parts[0].lower()
    if head == "/asks":
        pending = remote.list_pending()
        if not pending:
            print(f"  {_DIM}no pending approvals{_RESET}", file=sys.stderr)
        else:
            for a in pending:
                print(f"  {a.get('ask_id', '')[:8]}  {a.get('title', '')}", file=sys.stderr)
        return True
    if head in ("/approve", "/reject"):
        # No id → answer the active (oldest) pending ask. The id is resolved in
        # code so the user never has to type it; an explicit id still works for
        # answering out of order.
        target = parts[1] if len(parts) >= 2 else ""
        resp = "approve" if head == "/approve" else "reject"
        print(f"  {await remote.answer(target, resp)}", file=sys.stderr)
        return True
    if head == "/reply":
        if len(parts) < 3:
            print(f"  {_DIM}usage: /reply <id> <text>{_RESET}", file=sys.stderr)
            return True
        print(f"  {await remote.answer(parts[1], ' '.join(parts[2:]))}", file=sys.stderr)
        return True
    return False


def _handle_slash(
    cmd: str,
    *,
    session_id: str,
    workspace: Path,
    renderer: "ChatRenderer",
) -> Any:
    """Return _SLASH_QUIT to exit, _SLASH_HANDLED if handled in-place,
    None if the input isn't a slash command, or a string "new" sentinel
    for /new (so the caller can rotate the thread_id).
    """
    c = cmd.strip()
    if not c.startswith("/"):
        return None
    parts = c.split()
    head = parts[0].lower()
    if head in ("/quit", "/exit", "/q"):
        return _SLASH_QUIT
    if head == "/help":
        _print_help()
        return _SLASH_HANDLED
    if head == "/clear":
        # ANSI clear + redraw banner.
        print("\033[2J\033[H", end="", file=sys.stderr)
        _print_banner(session_id=session_id, workspace=workspace, resuming=False)
        return _SLASH_HANDLED
    if head == "/session":
        print(f"  {_DIM}session:{_RESET}   {session_id}", file=sys.stderr)
        print(f"  {_DIM}workspace:{_RESET} {workspace}", file=sys.stderr)
        return _SLASH_HANDLED
    if head == "/ring":
        renderer.print_ring()
        return _SLASH_HANDLED
    if head == "/last":
        k = 1
        if len(parts) > 1:
            try:
                k = int(parts[1])
            except ValueError:
                print(f"{_DIM}/last: expected integer, got {parts[1]!r}{_RESET}", file=sys.stderr)
                return _SLASH_HANDLED
        renderer.print_last(k)
        return _SLASH_HANDLED
    if head == "/expand":
        if len(parts) < 2:
            print(f"{_DIM}/expand: usage: /expand <n>{_RESET}", file=sys.stderr)
            return _SLASH_HANDLED
        try:
            idx = int(parts[1])
        except ValueError:
            print(f"{_DIM}/expand: expected integer, got {parts[1]!r}{_RESET}", file=sys.stderr)
            return _SLASH_HANDLED
        renderer.print_entry(idx)
        return _SLASH_HANDLED
    if head == "/new":
        return "new"
    print(f"{_DIM}unknown command: {head} (try /help){_RESET}", file=sys.stderr)
    return _SLASH_HANDLED


# ---------------------------------------------------------------------------
# Main REPL entrypoint
# ---------------------------------------------------------------------------


async def run_chat_repl(
    *,
    workspace: Path,
    settings: LlmSettings,
    execution_mode: str,
    docker_settings: DockerSettings,
    local_settings: LocalSettings,
    search_config: SearchConfig,
    bash_timeout_sec: int,
    recursion_limit: int,
    permission_check: bool,
    resume_id: str | None,
    continue_latest: bool,
    verbose: bool,
    debug_plumbing: bool = False,
) -> int:
    """Drive the interactive chat loop. Returns process exit code."""
    if not debug_plumbing:
        debug_plumbing = os.environ.get("YUYUTSAVA_DEBUG_PLUMBING", "").lower() in ("1", "true", "yes")
    if not debug_plumbing:
        silence_plumbing_loggers()

    store = get_default_session_store()
    sessions_settings = SessionsSettings.from_env()

    # History file lives under the standard YUYUTSAVA state dir so it
    # follows the same lifecycle as the SQLite session store.
    history_path = state_dir() / "chat_history"

    renderer = ChatRenderer(verbose=verbose, workspace=workspace)
    exit_code = 0

    # The renderer is the only voice the user should hear in chat mode.
    # Without this, the TaskRunner / tool_registry / task_runner.tools
    # INFO lines interleave with renderer output and look like duplicate
    # noise. Plumbing debugging keeps its escape hatch via the env var.
    if not debug_plumbing:
        import logging as _logging

        for _name in (
            "yuyutsava.task_runner",
            "yuyutsava.agents.task_runner.tools",
            "yuyutsava.core.tool_registry",
            "yuyutsava.core.permission_middleware",
        ):
            _logging.getLogger(_name).setLevel(_logging.WARNING)

    async with build_checkpointer(sessions_settings) as checkpointer:
        # Build the agent stack ONCE. Swallow the LangGraph host's startup
        # banner unless the user asked for the firehose.
        builder = build_cli_agent_stack(
            workspace,
            settings,
            bash_timeout_sec=bash_timeout_sec,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            docker_settings=docker_settings,
            local_settings=local_settings,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
        )
        # Always wrap build_cli_agent_stack in fd-level stdio suppression —
        # the LangGraph host writes its startup banner from a daemon thread
        # using direct fd writes that bypass Python logging. Skip only when
        # the user explicitly asked to see plumbing.
        if debug_plumbing:
            bundle = await builder
        else:
            with _suppress_stdio():
                bundle = await builder
            # langgraph_api re-imports `logging` inside run_server and resets
            # uvicorn handlers, so re-silence after the build.
            silence_plumbing_loggers()

        # Async-subagent HITL wiring.
        #   Preferred: when a daemon is running it owns the async host and the
        #   single, idempotent decision pipeline. The chat defers to it — consume
        #   the daemon's SSE and answer over REST — so a background approval can be
        #   answered from the CLI OR the UI and stays in sync, and the prompt never
        #   freezes (no competing stdin reader, no double-resume).
        #   Fallback: only when this chat OWNS the host (no daemon) do we run the
        #   legacy in-process watcher that prompts locally.
        cli_bridge = None
        cli_watcher = None
        cli_remote = None
        if bundle.async_host_url is not None:
            from yuyutsava.daemon.singleton import read_daemon_discovery
            disco = read_daemon_discovery()
            daemon_web = disco.get("web_url") if isinstance(disco, dict) else None
            if daemon_web:
                from yuyutsava.cli.async_hitl import CliRemoteHitl
                from yuyutsava.cli.remote_attach import CliAttachClient

                cli_remote = CliRemoteHitl(
                    CliAttachClient(base_url=_loopback_url(str(daemon_web)),
                                    label="yuyutsava-chat")
                )
                await cli_remote.start()
            elif bundle.async_task_mirror is not None:
                from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
                from yuyutsava.cli.async_hitl import CliHitlBridge

                cli_bridge = CliHitlBridge()
                cli_watcher = AsyncTaskHealthWatcher(
                    mirror=bundle.async_task_mirror,
                    host_url=bundle.async_host_url,
                    ask_handler=cli_bridge.post_ask,
                    event_sink=cli_bridge.post_event,
                    agent_path_root="cli",
                )
                await cli_watcher.start()

        try:
            # Resolve initial session (--resume / --continue / fresh) and wrap
            # it in the shared conversation engine. The terminal is just one IO
            # adapter over ConversationService — the daemon's text/voice chats
            # are others. ``ChatRenderer`` + ``_ask_handler`` below are this
            # adapter's output + HITL bridge.
            from yuyutsava.conversation import ConversationService

            convo, resuming = await ConversationService.resolve(
                store=store,
                bundle=bundle,
                workspace=workspace,
                origin="cli",
                resume_id=resume_id,
                continue_latest=continue_latest,
                agent_path="cli",
                recursion_limit=recursion_limit,
                task="(interactive chat)",
            )
            session = convo.session

            # Surface any langgraph-api upgrade/support notice once, cleanly,
            # right above the banner rather than mid-chat.
            _print_version_notice()
            _print_banner(
                session_id=session.id, workspace=workspace, resuming=resuming
            )

            # prompt_toolkit needs a TTY on stdin; when run with piped input
            # (tests, automation), fall back to plain blocking `input()` in a
            # thread so the REPL still works.
            is_tty = sys.stdin.isatty()
            # wrap_lines=True + full-width input area: the input editor spans
            # the whole terminal column count and wraps long lines instead of
            # scrolling horizontally inside a narrow gutter.
            prompt_session: PromptSession[str] | None = (
                PromptSession(
                    history=FileHistory(str(history_path)),
                    multiline=False,
                    wrap_lines=True,
                )
                if is_tty
                else None
            )

            async def _read_input() -> str:
                if prompt_session is not None:
                    # ANSI(...) wrapper: prompt_toolkit otherwise renders the
                    # raw escape bytes as visible characters (^[[36m…).
                    with patch_stdout():
                        return await prompt_session.prompt_async(
                            ANSI(f"\n{_CYAN}>{_RESET} ")
                        )
                # Non-TTY: run blocking input() in a worker thread.
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, lambda: input(f"\n{_CYAN}>{_RESET} ")
                )

            while True:
                # Flush any background-task events queued during the previous
                # turn so the user sees them before composing the next message.
                if cli_bridge is not None:
                    try:
                        await cli_bridge.render_between_turns()
                    except Exception:
                        pass

                try:
                    user_input = await _read_input()
                except (EOFError, KeyboardInterrupt):
                    # Ctrl+D or Ctrl+C at the empty prompt: clean exit.
                    print(file=sys.stderr)
                    break

                user_input = (user_input or "").strip()
                if not user_input:
                    continue

                # Background-approval commands (/asks, /approve, /reject, /reply)
                # are answered against the daemon — non-blocking, synced with the UI.
                if cli_remote is not None:
                    if await _handle_ask_command(user_input, cli_remote):
                        continue
                    # A bare decision word (y/n/yes/no/approve/reject/session/
                    # project/s/p) answers the ACTIVE (oldest) pending approval —
                    # the id is resolved in code. Only intercepted when something
                    # is actually pending, so normal messages pass through.
                    tok = _decision_token(user_input)
                    if tok is not None and cli_remote.list_pending():
                        print(f"  {await cli_remote.answer('', tok)}", file=sys.stderr)
                        remaining = len(cli_remote.list_pending())
                        if remaining:
                            print(f"  {_DIM}{remaining} more pending — "
                                  f"answer with y/n/s/p{_RESET}", file=sys.stderr)
                        continue

                slash_result = _handle_slash(
                    user_input, session_id=session.id, workspace=workspace, renderer=renderer,
                )
                if slash_result is _SLASH_QUIT:
                    break
                if slash_result is _SLASH_HANDLED:
                    continue
                if slash_result == "new":
                    # Rotate to a brand-new session row + thread_id in-process.
                    session = await convo.new_session(task="(interactive chat)")
                    _print_banner(
                        session_id=session.id, workspace=workspace, resuming=False
                    )
                    continue

                # Run one turn through the shared conversation engine. The
                # renderer is the terminal output adapter; _ask_handler is the
                # terminal HITL bridge.
                try:
                    await convo.run_turn(
                        user_input,
                        on_event=renderer.render,
                        ask_handler=_ask_handler,
                        run_name="cli-chat",
                        keep_full_payloads=True,
                    )
                except KeyboardInterrupt:
                    await renderer.end_of_turn()
                    print(
                        f"{_DIM}(turn cancelled — session still open){_RESET}",
                        file=sys.stderr,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    await renderer.end_of_turn()
                    print(f"{_RED}error:{_RESET} {exc}", file=sys.stderr)
                    continue

                await renderer.end_of_turn()

            # Loop exited — flush bookkeeping and mark the session done.
            try:
                await convo.finish("done")
            except Exception:
                pass

        finally:
            with contextlib.suppress(Exception):
                if renderer._smoother is not None:
                    await renderer._smoother.aclose()
            if cli_watcher is not None:
                try:
                    await cli_watcher.shutdown()
                except Exception:
                    pass
            if cli_remote is not None:
                with contextlib.suppress(Exception):
                    await cli_remote.stop()
            if execution_mode == "local" and bundle.sandbox_root is not None:
                try:
                    cleanup_local_sandbox(workspace, bundle.sandbox_root)
                except Exception:
                    pass
            await bundle.aclose()

    print(f"{_DIM}— chat closed —{_RESET}", file=sys.stderr)
    return exit_code
