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
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.engine import cleanup_local_sandbox, silence_plumbing_loggers
from yuyutsava.core.streaming import StreamEvent, _normalize_yes_no, astream_agent_iter
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

    Maintains a ring buffer of recent tool_call / tool_result events with
    their full bodies so the ``/last`` and ``/expand`` slash commands can
    show what the inline preview truncated.
    """

    def __init__(self, *, verbose: bool) -> None:
        self._verbose = verbose
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

    def render(self, ev: StreamEvent) -> None:
        if ev.kind == "token":
            if not self._in_ai_stream:
                # Open the AI line with a small chip; no big separator block.
                print(f"\n{_CYAN}🤖{_RESET}  ", end="", flush=True)
                self._in_ai_stream = True
            text = ev.data.get("text", "")
            print(text, end="", flush=True)
            return

        # Any non-token event closes the AI stream visually.
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

        if ev.kind == "tool_call":
            name = ev.data.get("name", "?")
            args = ev.data.get("args", {})
            preview = self._fmt_args(args, limit=120 if not self._verbose else 400)
            idx = self._push_ring("tool_call", name, self._pretty(args))
            print(
                f"  {_DIM}· {name}({preview}) [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )

        elif ev.kind == "tool_result":
            name = ev.data.get("name", "tool")
            body = ev.data.get("preview", "") or ""
            full = ev.data.get("full", body) or body
            limit = 600 if self._verbose else 200
            if len(body) > limit:
                body = body[:limit] + " …"
            # Collapse to one line for the compact display.
            one_line = body.replace("\n", " ⏎ ")
            idx = self._push_ring("tool_result", name, full)
            print(
                f"  {_DIM}↳ {name}: {one_line} [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )

        elif ev.kind == "log":
            text = ev.data.get("text", "")
            if text:
                print(f"{_YELLOW}{text}{_RESET}", file=sys.stderr, flush=True)

        elif ev.kind == "final":
            # The token stream above already covered the prose. Just newline.
            print(flush=True)

    def end_of_turn(self) -> None:
        """Force-close any dangling streaming line."""
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
    print(
        f"  {_DIM}[y]es / [n]o  (also: approve / reject){_RESET}",
        file=sys.stderr,
    )

    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: input("approve/reject> ").strip())
    except (EOFError, KeyboardInterrupt):
        return "reject"
    if not raw:
        return "reject"
    return _normalize_yes_no(raw)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


_SLASH_QUIT = object()
_SLASH_HANDLED = object()


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

    renderer = ChatRenderer(verbose=verbose)
    exit_code = 0

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

        # Wire the async-subagent HITL bridge if the bundle has a host URL —
        # this works whether the chat process owns the host or attached to
        # one started by the daemon / another chat.
        cli_bridge = None
        cli_watcher = None
        if bundle.async_host_url is not None and bundle.async_task_mirror is not None:
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
            # Resolve initial session: --resume / --continue / fresh.
            from yuyutsava.sessions.runner import _resolve_session  # internal but stable

            session, resuming = await _resolve_session(
                store,
                workspace=workspace,
                task="(interactive chat)",
                resume_id=resume_id,
                continue_latest=continue_latest,
            )

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

                slash_result = _handle_slash(
                    user_input, session_id=session.id, workspace=workspace, renderer=renderer,
                )
                if slash_result is _SLASH_QUIT:
                    break
                if slash_result is _SLASH_HANDLED:
                    continue
                if slash_result == "new":
                    # Rotate to a brand-new session row + thread_id in-process.
                    await store.update_status(session.id, "done")
                    session, _ = await _resolve_session(
                        store,
                        workspace=workspace,
                        task="(interactive chat)",
                        resume_id=None,
                        continue_latest=False,
                    )
                    _print_banner(
                        session_id=session.id, workspace=workspace, resuming=False
                    )
                    continue

                # Run one turn through the structured-event stream.
                try:
                    async for ev in astream_agent_iter(
                        bundle.agent,
                        user_input,
                        thread_id=session.thread_id,
                        recursion_limit=recursion_limit,
                        ask_handler=_ask_handler,
                        run_name="cli-chat",
                        agent_path="cli",
                        keep_full_payloads=True,
                    ):
                        renderer.render(ev)
                except KeyboardInterrupt:
                    renderer.end_of_turn()
                    print(
                        f"{_DIM}(turn cancelled — session still open){_RESET}",
                        file=sys.stderr,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    renderer.end_of_turn()
                    print(f"{_RED}error:{_RESET} {exc}", file=sys.stderr)
                    continue

                renderer.end_of_turn()

            # Loop exited — mark the session done.
            try:
                await store.update_status(session.id, "done")
            except Exception:
                pass

        finally:
            if cli_watcher is not None:
                try:
                    await cli_watcher.shutdown()
                except Exception:
                    pass
            if execution_mode == "local" and bundle.sandbox_root is not None:
                try:
                    cleanup_local_sandbox(workspace, bundle.sandbox_root)
                except Exception:
                    pass
            bundle.close()

    print(f"{_DIM}— chat closed —{_RESET}", file=sys.stderr)
    return exit_code
