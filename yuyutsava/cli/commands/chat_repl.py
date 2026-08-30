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
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from yuyutsava.cli.agent_stack import build_cli_agent_stack
from yuyutsava.cli.render.plain import (
    _CYAN,
    _DIM,
    _GREEN,
    _RED,
    _RESET,
    _YELLOW,
    ChatRenderer,
)
from yuyutsava.consent import decision_token as _decision_token
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.engine import cleanup_local_sandbox, silence_plumbing_loggers
from yuyutsava.prefs.runtime import UNDISABLEABLE
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



def _print_version_notice(*, full: bool = False) -> None:
    """Render langgraph-api upgrade/support notices once, above the banner.

    langgraph_api normally emits these from a background daemon thread that
    lands mid-chat (disabled via ``LANGGRAPH_NO_VERSION_CHECK`` in
    ``AsyncSubagentHost.start``). Here we run the *same* check synchronously so
    any notice appears cleanly before the YUYUTSAVA graphic instead of
    interleaving with the conversation. Best-effort — never breaks startup.

    The multi-line upgrade/EOL text is condensed to one dim line; ``full=True``
    (debug plumbing) keeps the verbatim notice.
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
    if full:
        for msg in lines:
            for line in msg.splitlines():
                print(f"{_DIM}{_YELLOW}{line}{_RESET}", file=sys.stderr)
        return
    # Condense the whole notice into one dim line: latest version + EOL flag.
    joined = "\n".join(lines)
    m = re.search(r"→\s*([0-9][0-9A-Za-z._-]*)", joined)
    latest = m.group(1) if m else None
    eol = "End of Life" in joined or "end of life" in joined.lower()
    parts = ["langgraph-api update available"]
    if latest:
        parts[0] += f" → {latest}"
    if eol:
        parts.append("current version is EOL")
    parts.append("pip install -U langgraph-api")
    print(f"  {_DIM}{_YELLOW}⚠ {' · '.join(parts)}{_RESET}", file=sys.stderr)


def _print_banner(
    *,
    session_id: str,
    workspace: Path,
    resuming: bool,
    status: str | None = None,
) -> None:
    print(file=sys.stderr)
    for line in _BANNER_LINES:
        print(f"{_CYAN}{line}{_RESET}", file=sys.stderr)
    verb = "resumed session" if resuming else "session"
    print(file=sys.stderr)
    print(f"  {_DIM}{verb}:{_RESET}  {session_id}", file=sys.stderr)
    print(f"  {_DIM}workspace:{_RESET} {workspace}", file=sys.stderr)
    if status:
        print(f"  {_DIM}{status}{_RESET}", file=sys.stderr)
    print(
        f"  {_DIM}type /help, /quit, or press Ctrl+D to exit{_RESET}",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def _startup_status_line(sessions_settings: SessionsSettings) -> str:
    """One dim line summarizing what the silenced startup logs used to say.

    Replaces the ``pg pool: open`` / ``pg migrations: at vN`` / ``Langfuse not
    active`` INFO lines with a compact ``storage · tracing · langgraph-api``
    summary. Best-effort — every probe degrades to a placeholder.
    """
    try:
        from yuyutsava.core import tracing as _tracing

        tracing_on = _tracing.is_configured() and _tracing._langfuse_reachable()
    except Exception:
        tracing_on = False
    try:
        from langgraph_api import __version__ as _lg_version
    except Exception:
        _lg_version = "?"
    return (
        f"storage: {sessions_settings.backend} · "
        f"tracing: {'on' if tracing_on else 'off'} · "
        f"langgraph-api {_lg_version}"
    )


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
    print(f"  {_DIM}/voice{_RESET}        voice mode: /voice on|off, /voice wake off, /voice tts off", file=sys.stderr)
    print(f"  {_DIM}/subagents{_RESET}    dedicated subagents: /subagents off face-watcher", file=sys.stderr)
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
# Renderers live in yuyutsava/cli/render/: ``plain.ChatRenderer`` (imported
# above) is the ANSI fallback; ``renderer.RichChatRenderer`` subclasses it
# for TTYs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Slash-command completion
# ---------------------------------------------------------------------------

_SLASH_COMMANDS: dict[str, str] = {
    "/help": "show available commands",
    "/quit": "exit the chat (Ctrl+D also works)",
    "/clear": "clear the screen and redraw the banner",
    "/new": "start a fresh session in this process",
    "/session": "show the current session id",
    "/ring": "list recent tool calls/results with their [#n] indices",
    "/last": "print the last k captured payloads in full",
    "/expand": "print the full body of the [#n] entry",
    "/asks": "list pending background approvals",
    "/approve": "approve a pending background ask",
    "/reject": "reject a pending background ask",
    "/reply": "answer a background question by id",
    "/voice": "show or set voice mode (/voice on|off|wake on|tts off)",
    "/subagents": "list dedicated subagents (/subagents on|off <name>)",
}


class _SlashCompleter(Completer):
    """Complete ``/commands`` (with descriptions) at the start of the line."""

    def get_completions(self, document, complete_event):  # noqa: ANN001, ANN201
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        lowered = text.lower()
        for cmd, desc in _SLASH_COMMANDS.items():
            if cmd.startswith(lowered):
                yield Completion(
                    cmd, start_position=-len(text), display_meta=desc
                )


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


def make_ask_handler(renderer: "ChatRenderer", console: Any = None):
    """Build the REPL's interrupt handler.

    ``renderer.pause()`` stops any live spinner region before the blocking
    ``input()`` (a repainting Live would fight the prompt) and restarts it
    after. With a rich ``console`` the card is a humanized Panel; the plain
    path keeps the ANSI card, now with the same plain-English headline.
    """

    async def _ask_handler(interrupt_value: Any) -> str:
        """Render a permission/question interrupt and read the user's reply."""
        payload = interrupt_value if isinstance(interrupt_value, dict) else {"text": str(interrupt_value)}
        itype = payload.get("type", "")
        loop = asyncio.get_running_loop()

        from yuyutsava.cli.render import panels

        with renderer.pause():
            print(file=sys.stderr)
            if itype == "user_question":
                if console is not None:
                    panels.print_ask_panel(console, payload)
                else:
                    print(f"{_YELLOW}? {panels.headline(payload)}{_RESET}", file=sys.stderr)
                    body = payload.get("body") or payload.get("question") or ""
                    if body:
                        print(f"  {body}", file=sys.stderr)
                try:
                    answer = await loop.run_in_executor(None, lambda: input("> ").strip())
                except (EOFError, KeyboardInterrupt):
                    return "reject"
                return answer or "no response"

            if console is not None:
                panels.print_ask_panel(console, payload)
            else:
                print(f"{_YELLOW}▣ {panels.headline(payload)}{_RESET}", file=sys.stderr)
                _render_permission_payload(payload)
                # Offer the allowlist scopes for every task-runner operation type
                # (matches the daemon's options_for_interrupt). [s]ession /
                # [p]roject remember the op for the whole workspace so it isn't
                # re-asked per file/subfolder.
                print(f"  {_DIM}{panels.options_hint(itype)}{_RESET}", file=sys.stderr)

            # Re-prompt on a blank / unrecognized line instead of silently
            # rejecting: with several parallel asks under prompt_toolkit, a stray
            # buffered line could otherwise be misread as a rejection. Explicit
            # reject words and EOF/Ctrl-C still reject; retries are capped so a
            # closed stdin can't spin forever.
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

    return _ask_handler


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


class _LazyRuntimeSettings:
    """Opens ``state.db`` only if the user actually types /voice or /subagents.

    The REPL has no reason to pay for the events ``Store`` (and its writer
    task) on every chat launch — these toggles are occasional. Built on first
    use and closed with :meth:`aclose` when the REPL exits.
    """

    def __init__(self) -> None:
        self._settings: Any | None = None
        self._store: Any | None = None

    async def get(self) -> Any:
        if self._settings is None:
            from yuyutsava.prefs.runtime import RuntimeSettings
            from yuyutsava.storage.events import Store
            from yuyutsava.storage.prefs import PrefsStore

            self._store = Store()
            await self._store.start()
            self._settings = await RuntimeSettings(PrefsStore(self._store)).load()
        return self._settings

    async def aclose(self) -> None:
        if self._store is not None:
            # Let a just-issued write drain before the store goes away (same
            # 50ms courtesy the `yuyutsava prefs` subcommand takes).
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                await self._store.stop()
            self._store = None
            self._settings = None


def _on_off(word: str) -> bool | None:
    w = word.strip().lower()
    if w in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if w in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    return None


async def _handle_settings_command(cmd: str, runtime_settings: Any) -> bool:
    """Handle ``/voice`` and ``/subagents`` (async — they hit ``state.db``).

    These write the same ``user_prefs`` rows the daemon and the desktop app
    read, so a toggle typed here reaches every surface without any CLI↔daemon
    transport (the daemon re-reads on a short TTL — see
    :class:`~yuyutsava.prefs.runtime.RuntimeSettings`).

    Returns True when *cmd* was one of these commands (and was handled).
    """
    c = cmd.strip()
    if not c.startswith("/"):
        return False
    parts = c.split()
    head = parts[0].lower()
    if head not in ("/voice", "/subagents"):
        return False

    await runtime_settings.refresh(force=True)

    if head == "/voice":
        args = [p.lower() for p in parts[1:]]
        # /voice on|off              → both switches
        # /voice wake on | tts off   → one switch
        if not args:
            pass
        elif len(args) == 1 and (flag := _on_off(args[0])) is not None:
            await runtime_settings.set_voice(wake_enabled=flag, tts_enabled=flag)
        elif len(args) == 2 and args[0] in ("wake", "tts") and (flag := _on_off(args[1])) is not None:
            key = "wake_enabled" if args[0] == "wake" else "tts_enabled"
            await runtime_settings.set_voice(**{key: flag})
        else:
            print(f"  {_DIM}usage: /voice [on|off] | /voice wake on|off | "
                  f"/voice tts on|off{_RESET}", file=sys.stderr)
            return True
        v = runtime_settings.voice()
        print(f"  {_DIM}wake word:{_RESET}    {'on' if v.wake_enabled else 'off'}", file=sys.stderr)
        print(f"  {_DIM}spoken reply:{_RESET} {'on' if v.tts_enabled else 'off'}", file=sys.stderr)
        if not v.wake_enabled:
            print(f"  {_DIM}(the mic is still yours to use manually){_RESET}", file=sys.stderr)
        return True

    # /subagents [on|off <name>]
    args = parts[1:]
    if args:
        if len(args) != 2 or (flag := _on_off(args[0])) is None:
            print(f"  {_DIM}usage: /subagents [on|off <name>]{_RESET}", file=sys.stderr)
            return True
        name = args[1]
        if name in UNDISABLEABLE and not flag:
            print(f"  {_DIM}{name} can't be switched off — the master delegates "
                  f"to it as a fallback{_RESET}", file=sys.stderr)
            return True
        await runtime_settings.set_subagent_enabled(name, flag)
    disabled = runtime_settings.subagents().disabled
    if disabled:
        print(f"  {_DIM}off:{_RESET} {', '.join(sorted(disabled))}", file=sys.stderr)
    else:
        print(f"  {_DIM}all dedicated subagents are on{_RESET}", file=sys.stderr)
    # This REPL's own roster is just general-purpose (the domain subagents live
    # in the daemon), so say what the toggle actually affects rather than
    # implying a local change that didn't happen.
    print(f"  {_DIM}applies to the daemon's orchestrator, chat and voice "
          f"agents{_RESET}", file=sys.stderr)
    return True


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

    # Rich transcript on real TTYs; the plain ANSI renderer for pipes and
    # dumb terminals stays byte-identical to the historical behavior.
    from yuyutsava.cli.render.console import make_console, rich_capable

    console = None
    if rich_capable():
        from yuyutsava.cli.render.renderer import RichChatRenderer

        console = make_console()
        renderer: ChatRenderer = RichChatRenderer(
            verbose=verbose, workspace=workspace, console=console
        )
    else:
        renderer = ChatRenderer(verbose=verbose, workspace=workspace)
    ask_handler = make_ask_handler(renderer, console)
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
            # Startup chatter folded into the banner status line instead:
            # "pg pool: open", "pg migrations: at vN", "Langfuse not active".
            # REPL-scoped on purpose — the daemon keeps these lines.
            "yuyutsava.storage.pg.pool",
            "yuyutsava.storage.pg.migrations",
            "yuyutsava.core.tracing",
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
        # /voice + /subagents backing store — opened on first use, not here.
        runtime_toggles = _LazyRuntimeSettings()
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
            _print_version_notice(full=debug_plumbing)
            status_line = _startup_status_line(sessions_settings)
            _print_banner(
                session_id=session.id,
                workspace=workspace,
                resuming=resuming,
                status=status_line,
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
                    completer=_SlashCompleter(),
                    complete_while_typing=True,
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

                # Runtime toggles (/voice, /subagents) — async because they
                # read/write state.db, which every other surface reads too.
                # Guarded so a normal message never opens the prefs store.
                if user_input.strip().startswith(("/voice", "/subagents")):
                    if await _handle_settings_command(
                        user_input, await runtime_toggles.get(),
                    ):
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
                        session_id=session.id,
                        workspace=workspace,
                        resuming=False,
                        status=status_line,
                    )
                    continue

                # Run one turn through the shared conversation engine. The
                # renderer is the terminal output adapter; _ask_handler is the
                # terminal HITL bridge.
                try:
                    renderer.begin_turn()
                    await convo.run_turn(
                        user_input,
                        on_event=renderer.render,
                        ask_handler=ask_handler,
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
                    # Name the exception type and keep the traceback: a bare
                    # str(exc) turns a library-internal failure ("list index out
                    # of range") into an unattributable one-liner, and the frame
                    # it came from is the only thing that makes it fixable. The
                    # turn still fails soft — the session stays open either way.
                    print(
                        f"{_RED}error:{_RESET} {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    tb = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )
                    print(f"{_DIM}{tb.rstrip()}{_RESET}", file=sys.stderr)
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
            await runtime_toggles.aclose()
            if execution_mode == "local" and bundle.sandbox_root is not None:
                try:
                    cleanup_local_sandbox(workspace, bundle.sandbox_root)
                except Exception:
                    pass
            await bundle.aclose()

    print(f"{_DIM}— chat closed —{_RESET}", file=sys.stderr)
    return exit_code
