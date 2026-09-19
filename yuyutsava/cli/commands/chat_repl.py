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
import importlib.metadata
import json
import logging
import os
import sys
import time
import traceback
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
from yuyutsava.cli.async_hitl import CliHitlBridge, CliRemoteHitl
from yuyutsava.cli.commands.options import ChatOptions
from yuyutsava.cli.line_reader import read_line
from yuyutsava.cli.remote_attach import CliAttachClient
from yuyutsava.cli.render import panels
from yuyutsava.cli.render.console import make_console, rich_capable
from yuyutsava.cli.render.context_panel import context_report, fmt_cost, fmt_pct, fmt_tokens
from yuyutsava.cli.render.dashboard import (
    ChatDashboard,
    dashboard_enabled,
    display_mode,
    terminal_cols,
    wide_enough,
)
from yuyutsava.cli.render.plain import (
    _CYAN,
    _DIM,
    _RED,
    _RESET,
    _YELLOW,
    ChatRenderer,
)
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.cli.tooling import cleanup_local_sandbox
from yuyutsava.config.env import env_flag
from yuyutsava.config.logs import silence_plumbing_loggers
from yuyutsava.consent import decision_token as _decision_token
from yuyutsava.context.meter import bus
from yuyutsava.conversation import ConversationService
from yuyutsava.core.agent_stack import build_agent_stack
from yuyutsava.daemon.singleton import read_daemon_discovery
from yuyutsava.llm import tracing as _tracing
from yuyutsava.llm.quirks.first_chunk_retry import set_retry_listener
from yuyutsava.prefs.runtime import UNDISABLEABLE, RuntimeSettings
from yuyutsava.skills.injector import last_recalled
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.paths import state_dir
from yuyutsava.storage.prefs import PrefsStore
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


_LANGGRAPH_API = "langgraph-api"


def _print_version_notice(*, full: bool = False) -> None:
    """Say once, above the banner, when langgraph-api has a newer release.

    langgraph_api has its own check, but it logs from a background thread that
    lands mid-chat (``AsyncSubagentHost.start`` turns it off with
    ``LANGGRAPH_NO_VERSION_CHECK``), and calling it directly means private API.
    This asks PyPI itself, with the same half-second timeout and the same
    end-of-life rule (:func:`_langgraph_api_is_eol`). A pre-release install is
    not checked. Best-effort — never breaks start-up.

    ``full=True`` (debug plumbing) also shows the installed version.
    """
    try:
        current = Version(importlib.metadata.version(_LANGGRAPH_API))
        url = f"https://pypi.org/pypi/{_LANGGRAPH_API}/json"
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            payload = json.load(resp)
        latest = Version(payload["info"]["version"])
        releases = payload.get("releases") or {}
    except Exception:  # noqa: BLE001 — not installed, offline, or an odd reply: no notice
        return
    if current.is_prerelease or latest <= current:
        return
    parts = [f"{_LANGGRAPH_API} update available → {latest}"]
    if full:
        parts[0] = f"{_LANGGRAPH_API} {current} → {latest} available"
    if _langgraph_api_is_eol(current, latest, releases):
        parts.append("current version is EOL")
    parts.append(f"pip install -U {_LANGGRAPH_API}")
    print(f"  {_DIM}{_YELLOW}⚠ {' · '.join(parts)}{_RESET}", file=sys.stderr)


def _langgraph_api_is_eol(current: Version, latest: Version, releases: Iterable[str]) -> bool:
    """langgraph-api's published support rule, applied to an installed version.

    End of life is two or more minor versions behind on the latest major, or
    an older major, except that while a new major is still at X.0 the last
    minor of the previous major is supported.
    """
    if current.major == latest.major:
        return current.minor <= latest.minor - 2
    if current.major == latest.major - 1 and latest.minor == 0:
        previous = []
        for raw in releases:
            try:
                version = Version(raw)
            except InvalidVersion:
                continue
            if version.major == current.major and not version.is_prerelease:
                previous.append(version)
        return not previous or current.minor != max(previous).minor
    return current.major < latest.major


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


# prompt_toolkit's FileHistory appends forever. It is only the up-arrow
# buffer — the agent never reads it and it never enters a prompt — but it
# accumulates every line the user has ever typed, so it is worth a ceiling.
_HISTORY_MAX_BYTES = 10 * 1024 * 1024


def quieten_chat_loggers(*, debug_plumbing: bool, verbose: bool) -> None:
    """Leave the renderer as the only voice in chat mode.

    Without this the TaskRunner, tool-registry and command-safety INFO lines
    interleave with renderer output and read as duplicate noise; retry chatter
    is worse, because it emits at WARNING and prints straight through the Live
    region. Both are shown through the renderer instead — the spinner and the
    panel's notices — so silencing them here loses nothing and
    ``--debug-plumbing`` / ``--verbose`` keep the escape hatch.

    REPL-scoped on purpose: the daemon keeps every one of these lines.
    """
    if not debug_plumbing:
        for _name in (
            "yuyutsava.task_runner",
            "yuyutsava.agents.task_runner.tools",
            "yuyutsava.discovery.tool_registry",
            "yuyutsava.policy.builtin.command_safety",
            # Startup chatter folded into the banner status line instead:
            # "pg pool: open", "pg migrations: at vN", "Langfuse not active".
            # REPL-scoped on purpose — the daemon keeps these lines.
            "yuyutsava.storage.pg.pool",
            "yuyutsava.storage.pg.migrations",
            "yuyutsava.llm.tracing",
        ):
            logging.getLogger(_name).setLevel(logging.WARNING)

        # Retry chatter is rendered on the spinner instead (note_retry above).
        # These two emit at WARNING, so the level filter above cannot reach
        # them — and between them a single 429 storm produced a dozen lines
        # through the Live region. --verbose keeps them.
        if not verbose:
            for _name in (
                "langchain_google_vertexai._retry",
                "yuyutsava.llm.retry",
            ):
                logging.getLogger(_name).setLevel(logging.ERROR)


@dataclass(frozen=True)
class ChatSurface:
    """Where the transcript goes, and who draws it.

    Three modes, most capable first: **dashboard** is the split screen, with the
    transcript left and a live context column right; **rich** is the scrolling
    transcript; **plain** is ANSI lines for pipes and dumb terminals, byte for
    byte what this printed before either of the others existed. Rich renders the
    transcript in all three — the dashboard only changes where it lands and who
    draws the status line.
    """

    mode: str
    console: Any | None
    dashboard: "ChatDashboard | None"
    renderer: "ChatRenderer"


def build_surface(
    workspace: Path,
    *,
    history_path: Path,
    verbose: bool,
    classic: bool,
) -> ChatSurface:
    """Pick a display mode and build the renderer for it."""
    console = None
    dashboard: ChatDashboard | None = None
    mode = display_mode(
        classic=classic,
        is_tty=sys.stdin.isatty(),
        rich=rich_capable(),
        enabled=dashboard_enabled(),
        wide=wide_enough(),
    )
    if mode == "dashboard":
        dashboard = ChatDashboard(history_path=history_path, completer=_SlashCompleter())
        console = dashboard.console
        renderer: ChatRenderer = RichChatRenderer(
            verbose=verbose, workspace=workspace, console=console, live=False
        )
        dashboard.attach_renderer(renderer)
    elif mode == "rich":
        console = make_console()
        renderer = RichChatRenderer(verbose=verbose, workspace=workspace, console=console)
    else:
        renderer = ChatRenderer(verbose=verbose, workspace=workspace)
    # Only the width can surprise someone: --classic and the env flag are
    # choices, a narrow terminal is not. Said once, before the banner, because
    # silently dropping to the single-pane view looks like a bug.
    if mode == "rich" and not classic and dashboard_enabled() and not wide_enough():
        print(
            f"{_DIM}terminal is {terminal_cols()} columns — too narrow to split; "
            f"using the classic transcript (resize and restart for the "
            f"context panel){_RESET}",
            file=sys.stderr,
        )
    return ChatSurface(mode=mode, console=console, dashboard=dashboard, renderer=renderer)


def _rotate_history(path: Path) -> None:
    """Keep ``chat_history`` under the size ceiling. Never raises.

    Rotates rather than truncates so the previous file is still there if the
    user wants it, and keeps the newest half in place so recent up-arrow
    history survives the rotation.
    """
    try:
        if not path.is_file() or path.stat().st_size <= _HISTORY_MAX_BYTES:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        path.replace(path.with_suffix(".1"))
        # FileHistory entries are "# <ts>" followed by "+<line>" blocks; cut on
        # an entry boundary so the kept tail parses.
        tail = text[len(text) // 2 :]
        marker = tail.find("\n# ")
        path.write_text(tail[marker + 1 :] if marker >= 0 else "", encoding="utf-8")
    except OSError:
        pass


def _startup_status_line(sessions_settings: SessionsSettings) -> str:
    """One dim line summarizing what the silenced startup logs used to say.

    Replaces the ``pg pool: open`` / ``pg migrations: at vN`` / ``Langfuse not
    active`` INFO lines with a compact ``storage · tracing · langgraph-api``
    summary. Best-effort — every probe degrades to a placeholder.
    """
    try:
        tracing_on = _tracing.is_configured() and _tracing.langfuse_reachable()
    except Exception:
        tracing_on = False
    try:
        lg_version = importlib.metadata.version(_LANGGRAPH_API)
    except importlib.metadata.PackageNotFoundError:
        lg_version = "?"
    # The EFFECTIVE backend, not SessionsSettings.backend. That field reads
    # YUYUTSAVA_SESSIONS_BACKEND, which only selects the checkpointer's own
    # default and is almost never set — so the banner announced "sqlite" while
    # every store and the checkpointer were on Postgres, and a whole session's
    # analysis started from the wrong assumption about where its data was.
    try:
        storage = (
            "postgres" if StorageSettings.from_env().is_postgres() else sessions_settings.backend
        )
    except Exception:
        storage = sessions_settings.backend
    return (
        f"storage: {storage} · "
        f"tracing: {'on' if tracing_on else 'off'} · "
        f"langgraph-api {lg_version}"
    )


def _print_help() -> None:
    print(file=sys.stderr)
    print(f"{_CYAN}Commands:{_RESET}", file=sys.stderr)
    width = max(len(c.signature) for c in SLASH_COMMANDS)
    group = SLASH_COMMANDS[0].group
    for cmd in SLASH_COMMANDS:
        if cmd.group != group:
            print(file=sys.stderr)
            group = cmd.group
        print(f"  {_DIM}{cmd.signature:<{width}}{_RESET}  {cmd.help}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"{_DIM}Ctrl+C cancels the current turn but keeps the session open.{_RESET}",
        file=sys.stderr,
    )
    print(
        f"{_DIM}In the split view: PgUp/PgDn or the wheel scrolls, End follows "
        f"the output again, Ctrl+G hides the context panel, Ctrl+L clears the "
        f"transcript. Typing during a turn queues your message; Ctrl+S "
        f"interrupts the turn and sends it now. Start with --classic for the "
        f"single-pane view.{_RESET}",
        file=sys.stderr,
    )
    print(file=sys.stderr)


# ---------------------------------------------------------------------------
# Log silencing
# ---------------------------------------------------------------------------
#
# The plumbing-logger floor + the warnings filter live in
# ``yuyutsava.config.logs.silence_plumbing_loggers`` so the same rules apply
# to the daemon. The REPL just calls it.


@contextlib.contextmanager
def _suppress_stdio():
    """Temporarily route fd 1 / fd 2 to /dev/null.

    Used around ``build_agent_stack()`` to catch the LangGraph server
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


@dataclass(frozen=True)
class SlashCommand:
    """One ``/command`` as the user meets it.

    ``group`` only decides where a blank line falls in ``/help``; commands that
    do unrelated jobs read better apart.
    """

    name: str
    help: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    group: int = 0

    @property
    def signature(self) -> str:
        return f"{self.name} {self.usage}".strip()


#: Every slash command, once. ``/help`` and the tab-completer are both
#: generated from this, because they used to be two hand-kept lists and drifted:
#: the four background-approval commands were offered by completion and missing
#: from ``/help``, so the only way to find out how to answer a pending ask was
#: to press Tab and guess.
SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "show this help"),
    SlashCommand("/quit", "exit the chat (Ctrl+D also works)", aliases=("/exit", "/q")),
    SlashCommand("/clear", "clear the screen and redraw the banner"),
    SlashCommand("/new", "start a fresh session in this process"),
    SlashCommand("/session", "show the current session id"),
    SlashCommand("/ring", "list recent tool calls/results with their [#n] indices"),
    SlashCommand("/last", "print the last k captured payloads in full (default 1)", usage="[k]"),
    SlashCommand("/expand", "print the full body of the [#n] entry", usage="<n>"),
    SlashCommand("/asks", "list pending background approvals", group=1),
    SlashCommand("/approve", "approve a pending background ask", usage="[id]", group=1),
    SlashCommand("/reject", "reject a pending background ask", usage="[id]", group=1),
    SlashCommand("/reply", "answer a background question by id", usage="<id> <text>", group=1),
    SlashCommand("/voice", "voice mode: /voice on|off, /voice wake off, /voice tts off", group=2),
    SlashCommand("/subagents", "dedicated subagents: /subagents off face-watcher", group=2),
    SlashCommand("/usage", "tokens and estimated cost for this session", group=2),
    SlashCommand("/context", "what is in the context window right now, by segment", group=2),
    SlashCommand("/skills", "skills recalled this turn, and all available ones", group=2),
)

#: name → description, for the completer.
_SLASH_COMMANDS: dict[str, str] = {c.name: c.help for c in SLASH_COMMANDS}


class _SlashCompleter(Completer):
    """Complete ``/commands`` (with descriptions) at the start of the line."""

    def get_completions(self, document, complete_event):  # noqa: ANN001, ANN201
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        lowered = text.lower()
        for cmd, desc in _SLASH_COMMANDS.items():
            if cmd.startswith(lowered):
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


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

    The read goes through ``cli.line_reader.read_line``, never ``input()``:
    in the split view a prompt_toolkit application holds stdin in raw mode for
    the whole session, so a blocking read anywhere else in the process never
    sees a keystroke. The card rendered, ``approve/reject>`` appeared in the
    pane, and every key went to the application instead — the prompt could not
    be answered at all.
    """

    async def _ask_handler(interrupt_value: Any) -> str:
        """Render a permission/question interrupt and read the user's reply."""
        payload = (
            interrupt_value if isinstance(interrupt_value, dict) else {"text": str(interrupt_value)}
        )
        itype = payload.get("type", "")

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
                answer = await read_line("answer> ")
                if answer is None:
                    return "reject"
                return answer.strip() or "no response"

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
                raw = await read_line("approve/reject> ")
                if raw is None:
                    return "reject"
                raw = raw.strip()
                if not raw:
                    continue
                token = _decision_token(raw)
                if token is not None:
                    return token
                print(
                    f"  {_DIM}please answer: [y]es / [n]o / [s]ession / [p]roject{_RESET}",
                    file=sys.stderr,
                )
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


async def _usage_rows(store: Any, thread_id: str, since: float) -> list[Any]:
    """This thread's ``llm_usage`` rows since *since*. Never raises.

    ``UsageStore.list`` takes a ``thread_id`` filter now, so this no longer
    fetches 2,000 rows by time and drops the ones that belong to other
    conversations — a workaround that silently truncated on a busy machine.
    """
    if store is None or not thread_id:
        return []
    try:
        return await store.list(thread_id=thread_id, since=since, limit=2_000)
    except Exception:  # noqa: BLE001 — reporting must never break the REPL
        logging.getLogger("yuyutsava.cli.usage").debug("usage read failed", exc_info=True)
        return []


def _fmt_usage(rows: list[Any]) -> str:
    """``3 calls · in 41,087 (↺93%) · out 64 · ~$0.0032``.

    Empty when there is nothing to say. The cache share is shown only when the
    provider actually reported one: a 0 there means "no cache detail
    reported", which is not the same as a 0 % hit rate.
    """
    if not rows:
        return ""
    calls = len(rows)
    tin = sum(r.input_tokens for r in rows)
    tout = sum(r.output_tokens for r in rows)
    cost = sum(r.est_cost_usd for r in rows)
    cached = sum(getattr(r, "cache_read_tokens", 0) for r in rows)
    cache_note = f" (↺{cached / tin:.0%})" if cached and tin else ""
    out = f"{calls} call{'s' if calls != 1 else ''} · in {tin:,}{cache_note} · out {tout:,}"
    # A zero estimate means the model is missing from the price table, which
    # is not the same as free — say nothing rather than "$0.00".
    return f"{out} · ~${cost:,.4f}" if cost > 0 else out


async def _print_usage_summary(store: Any, thread_id: str, since: float) -> None:
    """``/usage``: session totals, broken down by model."""
    rows = await _usage_rows(store, thread_id, since)
    if not rows:
        print(
            f"  {_DIM}no recorded model calls yet in this session{_RESET}",
            file=sys.stderr,
        )
        return
    print(f"  {_DIM}session: {_fmt_usage(rows)}{_RESET}", file=sys.stderr)
    by_model: dict[str, list[Any]] = {}
    for r in rows:
        by_model.setdefault(r.model or "?", []).append(r)
    if len(by_model) > 1:
        for model, subset in sorted(
            by_model.items(), key=lambda kv: -sum(r.input_tokens for r in kv[1])
        ):
            print(f"    {_DIM}{model}: {_fmt_usage(subset)}{_RESET}", file=sys.stderr)
    peak = max(r.input_tokens for r in rows)
    print(
        f"    {_DIM}largest single request: {peak:,} input tokens{_RESET}",
        file=sys.stderr,
    )


def _print_skills(layout: Any) -> None:
    """``/skills``: what was recalled last turn, and what is available at all.

    Retrieval happens inside the graph, so until now there was no way to tell
    whether a skill reached the prompt — which matters, because a skill the
    agent never sees is indistinguishable from one that does not exist.
    """

    recalled = last_recalled()
    if recalled:
        print(f"  {_CYAN}recalled last turn:{_RESET} {', '.join(recalled)}", file=sys.stderr)
    else:
        print(
            f"  {_DIM}nothing recalled yet this session (the block is built per "
            f"turn from the message text){_RESET}",
            file=sys.stderr,
        )
    try:
        registry = (
            SkillRegistry(workspace_dir=layout.skills) if layout is not None else SkillRegistry()
        )
        skills = registry.scan(agent="cli")
    except Exception:  # noqa: BLE001 — a listing must never break the REPL
        return
    if not skills:
        return
    print(f"  {_DIM}available ({len(skills)}):{_RESET}", file=sys.stderr)
    for s in sorted(skills, key=lambda s: (s.scope, s.name)):
        mark = "*" if s.name in recalled else " "
        print(f"   {mark} {_DIM}{s.scope:9}{_RESET} {s.name}", file=sys.stderr)
    print(
        f"  {_DIM}the agent can also search these itself with sk_search_skill{_RESET}",
        file=sys.stderr,
    )


def _print_context(thread_id: str, console: Any = None) -> None:
    """``/context``: what is in the window, and what it has cost.

    Reads the latest snapshot the context meter published for this thread. The
    dashboard shows a condensed version of the same numbers continuously; this
    is the view with room to say which of them are estimates and why.
    """

    snap = bus().latest(thread_id)
    if console is not None:
        try:
            console.print(context_report(snap))
            return
        except Exception:  # noqa: BLE001 — fall through to the plain summary
            pass
    if snap is None:
        print(
            f"  {_DIM}no model call has completed yet — context figures appear "
            f"after the first one{_RESET}",
            file=sys.stderr,
        )
        return

    print(
        f"  {_DIM}window:{_RESET} {snap.used_tokens:,} / "
        f"{snap.max_input_tokens:,} ({fmt_pct(snap.used_fraction)})",
        file=sys.stderr,
    )
    for _key, label, tokens in snap.segments():
        print(f"    {_DIM}{label:<16}{_RESET} ≈{tokens:,}", file=sys.stderr)
    print(
        f"  {_DIM}last call:{_RESET} in {snap.input_tokens:,} · "
        f"out {snap.output_tokens:,} · "
        f"{fmt_cost(snap.est_cost_usd, snap.priced)}",
        file=sys.stderr,
    )
    print(
        f"  {_DIM}session:{_RESET} {snap.calls} calls · "
        f"in {fmt_tokens(snap.session_input_tokens)} · "
        f"{snap.compactions} compactions · {snap.offloads} offloaded",
        file=sys.stderr,
    )


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
        elif (
            len(args) == 2 and args[0] in ("wake", "tts") and (flag := _on_off(args[1])) is not None
        ):
            key = "wake_enabled" if args[0] == "wake" else "tts_enabled"
            await runtime_settings.set_voice(**{key: flag})
        else:
            print(
                f"  {_DIM}usage: /voice [on|off] | /voice wake on|off | /voice tts on|off{_RESET}",
                file=sys.stderr,
            )
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
            print(
                f"  {_DIM}{name} can't be switched off — the master delegates "
                f"to it as a fallback{_RESET}",
                file=sys.stderr,
            )
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
    print(
        f"  {_DIM}applies to the daemon's orchestrator, chat and voice agents{_RESET}",
        file=sys.stderr,
    )
    return True


class Outcome(Enum):
    """What the loop should do with a line it handed to the dispatcher."""

    NOT_A_COMMAND = auto()
    HANDLED = auto()
    QUIT = auto()


@dataclass
class ReplCommands:
    """Runs whatever the user typed that is not a message for the agent.

    The five dispatch styles this replaces — two handlers returning ``bool``,
    one returning sentinels, and three commands compared inline in the loop —
    meant the loop had to know each one's calling convention, and a new command
    could be added without the loop ever asking about it. One `dispatch` now
    answers a single question: was this line a command, and should we stop?

    ``session`` is mutable because ``/new`` replaces it; the loop reads it back
    from here afterwards rather than keeping its own copy.
    """

    workspace: Path
    console: Any
    renderer: Any
    bundle: Any
    convo: Any
    runtime_toggles: Any
    status_line: str
    session: Any
    remote: Any | None = None

    async def dispatch(self, text: str) -> Outcome:
        line = text.strip()
        if self.remote is not None:
            if await _handle_ask_command(line, self.remote):
                return Outcome.HANDLED
            if await self._answered_a_pending_ask(line):
                return Outcome.HANDLED
        if not line.startswith("/"):
            return Outcome.NOT_A_COMMAND
        head = line.split()[0].lower()
        if head in ("/voice", "/subagents"):
            # Async because they read and write state.db, which every other
            # surface reads too. Reached only for these two so a normal message
            # never opens the prefs store.
            await _handle_settings_command(line, await self.runtime_toggles.get())
            return Outcome.HANDLED
        if head == "/usage":
            await _print_usage_summary(
                self.bundle.usage_store, self.session.thread_id, self.session.created_at
            )
            return Outcome.HANDLED
        if head == "/context":
            _print_context(self.session.thread_id, self.console)
            return Outcome.HANDLED
        if head == "/skills":
            _print_skills(self.bundle.layout)
            return Outcome.HANDLED
        if head == "/new":
            await self._rotate_session()
            return Outcome.HANDLED
        result = _handle_slash(
            line,
            session_id=self.session.id,
            workspace=self.workspace,
            renderer=self.renderer,
        )
        return Outcome.QUIT if result is _SLASH_QUIT else Outcome.HANDLED

    async def _answered_a_pending_ask(self, line: str) -> bool:
        """A bare decision word answers the oldest pending approval.

        ``y``/``n``/``approve``/``session``/``p`` and friends, with the id
        resolved in code so the user never types one. Only intercepted while
        something is actually pending, so an ordinary message saying "no" still
        reaches the agent.
        """
        token = _decision_token(line)
        if token is None or not self.remote.list_pending():
            return False
        print(f"  {await self.remote.answer('', token)}", file=sys.stderr)
        remaining = len(self.remote.list_pending())
        if remaining:
            print(
                f"  {_DIM}{remaining} more pending — answer with y/n/s/p{_RESET}",
                file=sys.stderr,
            )
        return True

    async def _rotate_session(self) -> None:
        """``/new``: a brand-new session row and thread_id, in this process."""
        self.session = await self.convo.new_session(task="(interactive chat)")
        _print_banner(
            session_id=self.session.id,
            workspace=self.workspace,
            resuming=False,
            status=self.status_line,
        )


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
    opts: ChatOptions,
    *,
    debug_plumbing: bool = False,
    classic: bool = False,
) -> int:
    """Drive the interactive chat loop. Returns process exit code.

    ``debug_plumbing`` and ``classic`` stay separate because they are this
    surface's own: how much the terminal shows, not what the agent is.
    """
    workspace = opts.workspace
    settings = opts.settings
    execution_mode = opts.execution_mode
    docker_settings = opts.docker_settings
    local_settings = opts.local_settings
    search_config = opts.search_config
    bash_timeout_sec = opts.bash_timeout_sec
    recursion_limit = opts.recursion_limit
    permission_check = opts.permission_check
    resume_id = opts.resume_id
    continue_latest = opts.continue_latest
    verbose = opts.verbose
    if not debug_plumbing:
        debug_plumbing = env_flag("YUYUTSAVA_DEBUG_PLUMBING", true=("1", "true", "yes"))
    if not debug_plumbing:
        silence_plumbing_loggers()

    store = get_default_session_store()
    sessions_settings = SessionsSettings.from_env()

    # History file lives under the standard YUYUTSAVA state dir so it
    # follows the same lifecycle as the SQLite session store.
    history_path = state_dir() / "chat_history"
    _rotate_history(history_path)

    surface = build_surface(workspace, history_path=history_path, verbose=verbose, classic=classic)
    console, dashboard, renderer = surface.console, surface.dashboard, surface.renderer
    ask_handler = make_ask_handler(renderer, console)
    exit_code = 0

    # Provider retries (429/503) speak through the renderer, not the log. The
    # SDK's own retry logger emits several structlog lines per attempt, which
    # printed straight through the Live region and still left the user unable
    # to tell whether the turn was alive. Cleared in the finally block below.
    def _on_retry(model, attempt, retries, delay, exc):
        """Retries speak on the status line AND in the panel's notices.

        The status line is transient and easy to miss during a long wait; the
        panel keeps it up for the length of the backoff. Neither writes into
        the transcript, which is what the user asked for.
        """
        renderer.note_retry(model, attempt, retries, delay, exc)
        if dashboard is not None:
            dashboard.note_retry(model, attempt, retries, delay, exc)

    set_retry_listener(_on_retry)

    quieten_chat_loggers(debug_plumbing=debug_plumbing, verbose=verbose)

    async with build_checkpointer(sessions_settings) as checkpointer:
        # Build the agent stack ONCE. Swallow the LangGraph host's startup
        # banner unless the user asked for the firehose.
        builder = build_agent_stack(
            workspace,
            settings,
            bash_timeout_sec=bash_timeout_sec,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            docker_settings=docker_settings,
            local_settings=local_settings,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
            # This reply is going to a terminal, not the desktop app: no
            # Artifacts tab, no card to click, HTML unreadable.
            front="terminal",
        )
        # Always wrap build_agent_stack in fd-level stdio suppression —
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
            disco = read_daemon_discovery()
            daemon_web = disco.get("web_url") if isinstance(disco, dict) else None
            if daemon_web:
                cli_remote = CliRemoteHitl(
                    CliAttachClient(base_url=_loopback_url(str(daemon_web)), label="yuyutsava-chat")
                )
                await cli_remote.start()
            elif bundle.async_task_mirror is not None:
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

            # The split view must be up BEFORE anything is printed: it takes
            # the alternate screen, which wipes whatever is already on the
            # terminal. The banner used to be drawn first and then erased, so
            # `yuyutsava chat` opened on a bare screen with a stray Google
            # credentials warning at the top.
            if dashboard is not None:
                await dashboard.start()

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

            commands = ReplCommands(
                workspace=workspace,
                console=console,
                renderer=renderer,
                bundle=bundle,
                convo=convo,
                runtime_toggles=runtime_toggles,
                status_line=status_line,
                session=session,
                remote=cli_remote,
            )

            # prompt_toolkit needs a TTY on stdin; when run with piped input
            # (tests, automation), fall back to plain blocking `input()` in a
            # thread so the REPL still works.
            is_tty = sys.stdin.isatty()
            # wrap_lines=True + full-width input area: the input editor spans
            # the whole terminal column count and wraps long lines instead of
            # scrolling horizontally inside a narrow gutter.
            #
            # The dashboard owns its own input line inside the application
            # layout (same history file and completer), so it needs no session
            # here — a PromptSession would fight it for the terminal.
            prompt_session: PromptSession[str] | None = (
                PromptSession(
                    history=FileHistory(str(history_path)),
                    multiline=False,
                    wrap_lines=True,
                    completer=_SlashCompleter(),
                    complete_while_typing=True,
                )
                if is_tty and dashboard is None
                else None
            )

            async def _read_input() -> str:
                if dashboard is not None:
                    line = await dashboard.read_input()
                    if line is None:
                        raise EOFError  # Ctrl+D: the existing clean-exit path
                    return line
                if prompt_session is not None:
                    # ANSI(...) wrapper: prompt_toolkit otherwise renders the
                    # raw escape bytes as visible characters (^[[36m…).
                    with patch_stdout():
                        return await prompt_session.prompt_async(ANSI(f"\n{_CYAN}>{_RESET} "))
                # Non-TTY: run blocking input() in a worker thread.
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: input(f"\n{_CYAN}>{_RESET} "))

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

                outcome = await commands.dispatch(user_input)
                if outcome is Outcome.QUIT:
                    break
                if outcome is Outcome.HANDLED:
                    # `/new` may have rotated the session out from under us.
                    session = commands.session
                    continue

                # Run one turn through the shared conversation engine. The
                # renderer is the terminal output adapter; _ask_handler is the
                # terminal HITL bridge.
                turn_started = time.time()
                try:
                    renderer.begin_turn()
                    if dashboard is not None:
                        # In raw mode Ctrl+C is a key event, not SIGINT, so the
                        # dashboard cancels this task by hand and the handler
                        # below (task.uncancel) does the rest.
                        dashboard.set_turn_task(asyncio.current_task())
                    await convo.run_turn(
                        user_input,
                        on_event=renderer.render,
                        ask_handler=ask_handler,
                        run_name="cli-chat",
                        keep_full_payloads=True,
                    )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # Ctrl+C during a turn does NOT arrive as KeyboardInterrupt
                    # here: asyncio.Runner installs a SIGINT handler that
                    # cancels the main task, so the await raises
                    # CancelledError — which `except Exception` below does not
                    # catch either (it is a BaseException). It escaped to the
                    # runner, which re-raised KeyboardInterrupt, and the user
                    # got a 20-frame traceback instead of their prompt back.
                    #
                    # uncancel() clears the request so the REPL can keep
                    # awaiting; without it the next await re-raises at once and
                    # the runner still turns the cancelled task into a
                    # KeyboardInterrupt exit.
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
                    await renderer.end_of_turn()
                    print(
                        f"{_DIM}(turn cancelled — session still open){_RESET}",
                        file=sys.stderr,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    await renderer.end_of_turn()
                    # Provider capacity is not a bug in this program, and its
                    # traceback is 30 frames of SDK internals that say nothing
                    # the user can act on. One line, and the session stays
                    # open so the message can simply be resent.
                    if type(exc).__name__ in ("ResourceExhausted", "ServiceUnavailable"):
                        code = "429" if type(exc).__name__ == "ResourceExhausted" else "503"
                        print(
                            f"{_RED}provider busy ({code}){_RESET} — retries gave up. "
                            f"{_DIM}Resend to try again; VERTEX_RETRY_BUDGET_SEC (default 90) "
                            f"and VERTEX_MAX_RETRIES control how long it keeps trying. "
                            f"Session still open.{_RESET}",
                            file=sys.stderr,
                        )
                        continue
                    # Otherwise name the exception type and keep the traceback:
                    # a bare str(exc) turns a library-internal failure ("list
                    # index out of range") into an unattributable one-liner, and
                    # the frame it came from is the only thing that makes it
                    # fixable. The turn still fails soft either way.
                    print(
                        f"{_RED}error:{_RESET} {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    print(f"{_DIM}{tb.rstrip()}{_RESET}", file=sys.stderr)
                    continue

                await renderer.end_of_turn()
                if dashboard is not None:
                    # Turn over: Ctrl+C goes back to meaning "clear the line".
                    dashboard.set_turn_task(None)

                # What the turn cost, from the rows the graph just wrote. The
                # input count climbs with the conversation (the whole history
                # is re-sent every call), and that was invisible from inside a
                # session until now.
                _turn_usage = _fmt_usage(
                    await _usage_rows(bundle.usage_store, session.thread_id, turn_started)
                )
                if _turn_usage:
                    print(f"{_DIM}  {_turn_usage}{_RESET}", file=sys.stderr)

            # Loop exited — flush bookkeeping and mark the session done.
            try:
                await convo.finish("done")
            except Exception:
                pass

        finally:
            # The listener is process-global; a renderer that outlives this
            # REPL would keep drawing on a dead Live region.
            with contextlib.suppress(Exception):
                set_retry_listener(None)
            if dashboard is not None:
                # Leave the alternate screen first, then hand the transcript
                # back: the app takes the session's output with it when it
                # exits, and a user who just spent an hour in here should not
                # find an empty terminal.
                with contextlib.suppress(Exception):
                    await dashboard.stop()
                dashboard.replay_to(sys.stderr)
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
            await bundle.aclose()
            # After teardown: in docker mode the container is gone, so the
            # sandbox is no longer anyone's cwd — wipe scratch in either mode.
            if bundle.layout is not None:
                try:
                    cleanup_local_sandbox(bundle.layout)
                except Exception:
                    pass

    print(f"{_DIM}— chat closed —{_RESET}", file=sys.stderr)
    return exit_code
