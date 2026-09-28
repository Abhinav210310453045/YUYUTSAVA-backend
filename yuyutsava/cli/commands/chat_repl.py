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
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from yuyutsava.ansi import CLEAR_SCREEN, CYAN, DIM, RED, RESET, YELLOW
from yuyutsava.cli.background_approvals import BackgroundApprovals
from yuyutsava.cli.commands.options import ChatOptions
from yuyutsava.cli.line_reader import read_line
from yuyutsava.cli.render import panels, reports, startup
from yuyutsava.cli.render.console import make_console, rich_capable
from yuyutsava.cli.render.dashboard import (
    ChatDashboard,
    dashboard_enabled,
    display_mode,
    terminal_cols,
    wide_enough,
)
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.protocol import ChatRendererProtocol
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.cli.tooling import cleanup_local_sandbox
from yuyutsava.cli.usage import format_usage, session_usage
from yuyutsava.config.env import env_flag
from yuyutsava.config.logs import silence_plumbing_loggers
from yuyutsava.consent import decision_token as _decision_token
from yuyutsava.conversation import ConversationService
from yuyutsava.core.agent_stack import build_agent_stack
from yuyutsava.core.app_context import open_events_store
from yuyutsava.core.deps import StackWiring
from yuyutsava.llm.quirks.first_chunk_retry import add_retry_listener, classify_provider_busy
from yuyutsava.prefs.runtime import UNDISABLEABLE, RuntimeSettings
from yuyutsava.storage.paths import state_dir
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.sessions import (
    SessionsSettings,
    build_checkpointer,
    session_store_for,
)

logger = logging.getLogger("yuyutsava.cli.commands.chat_repl")

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

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


async def run_one_turn(
    convo: Any,
    text: str,
    *,
    renderer: "ChatRenderer",
    ask_handler: Any,
    dashboard: "ChatDashboard | None",
    retry: bool = False,
) -> bool:
    """Run one turn and fail soft. False when it did not finish.

    ``retry`` sends nothing: the model runs again on the history (``/retry``).

    The renderer is the terminal's output adapter and ``ask_handler`` its HITL
    bridge; everything else here is about not ending the session over a turn.
    The caller skips the cost summary on False — there is nothing to report for
    a turn that was cancelled or never reached the model.
    """
    try:
        renderer.begin_turn()
        if dashboard is not None:
            # In raw mode Ctrl+C is a key event, not SIGINT, so the dashboard
            # cancels this task by hand and the handler below does the rest.
            dashboard.set_turn_task(asyncio.current_task())
        await convo.run_turn(
            text,
            on_event=renderer.render,
            ask_handler=ask_handler,
            run_name="cli-chat",
            keep_full_payloads=True,
            retry=retry,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C during a turn does NOT arrive as KeyboardInterrupt here:
        # asyncio.Runner installs a SIGINT handler that cancels the main task,
        # so the await raises CancelledError — which `except Exception` below
        # does not catch either, it being a BaseException. It escaped to the
        # runner, which re-raised KeyboardInterrupt, and the user got a
        # 20-frame traceback instead of their prompt back.
        #
        # uncancel() clears the request so the REPL can keep awaiting; without
        # it the next await re-raises at once and the runner still turns the
        # cancelled task into a KeyboardInterrupt exit.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        await renderer.end_of_turn()
        print(f"{DIM}(turn cancelled — session still open){RESET}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001 — a failed turn must not end the session
        await renderer.end_of_turn()
        _report_turn_failure(exc)
        return False

    await renderer.end_of_turn()
    if dashboard is not None:
        # Turn over: Ctrl+C goes back to meaning "clear the line".
        dashboard.set_turn_task(None)
    return True


def _report_turn_failure(exc: BaseException) -> None:
    """Say what went wrong in the terms the reader can act on."""
    if type(exc).__name__ in ("ResourceExhausted", "ServiceUnavailable"):
        # Provider capacity is not a bug in this program, and its traceback is
        # 30 frames of SDK internals that say nothing the user can act on. One
        # line, and the session stays open so the message can simply be resent.
        code = classify_provider_busy(exc)
        print(
            f"{RED}provider busy ({code}){RESET} — retries gave up. "
            f"{DIM}Resend to try again; VERTEX_RETRY_BUDGET_SEC (default 90) "
            f"and VERTEX_MAX_RETRIES control how long it keeps trying. "
            f"Session still open.{RESET}",
            file=sys.stderr,
        )
        return
    # Otherwise name the exception type and keep the traceback: a bare str(exc)
    # turns a library-internal failure ("list index out of range") into an
    # unattributable one-liner, and the frame it came from is the only thing
    # that makes it fixable.
    print(f"{RED}error:{RESET} {type(exc).__name__}: {exc}", file=sys.stderr)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"{DIM}{tb.rstrip()}{RESET}", file=sys.stderr)


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
        renderer: ChatRendererProtocol = RichChatRenderer(
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
            f"{DIM}terminal is {terminal_cols()} columns — too narrow to split; "
            f"using the classic transcript (resize and restart for the "
            f"context panel){RESET}",
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
# Renderers live in yuyutsava/cli/render/: ``plain.ChatRenderer`` is the ANSI
# fallback and ``renderer.RichChatRenderer`` the TTY one. Neither derives from
# the other — both satisfy ``render.protocol.ChatRendererProtocol``, which is
# what this module is typed against.
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
#: generated from this, so they cannot disagree. Two hand-kept lists did: the
#: four background-approval commands were offered by completion and missing
#: from ``/help``, leaving no way to find out how to answer a pending ask short
#: of pressing Tab and guessing.
SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "show this help"),
    SlashCommand("/quit", "exit the chat (Ctrl+D also works)", aliases=("/exit", "/q")),
    SlashCommand("/clear", "clear the screen and redraw the banner"),
    SlashCommand("/new", "start a fresh session in this process"),
    SlashCommand("/retry", "run the model again on your last message, sending nothing new"),
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
                    print(f"{YELLOW}? {panels.headline(payload)}{RESET}", file=sys.stderr)
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
                print(f"{YELLOW}▣ {panels.headline(payload)}{RESET}", file=sys.stderr)
                reports.print_permission_payload(payload)
                # Offer the allowlist scopes for every task-runner operation type
                # (matches the daemon's options_for_interrupt). [s]ession /
                # [p]roject remember the op for the whole workspace so it isn't
                # re-asked per file/subfolder.
                print(f"  {DIM}{panels.options_hint(itype)}{RESET}", file=sys.stderr)

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
                # The same hint the card printed, not a second spelling of
                # it: the hardcoded one offered [s]ession and [p]roject for
                # every interrupt type, including the ones that cannot store
                # a grant, so it taught keys that do nothing.
                print(
                    f"  {DIM}please answer: {panels.options_hint(itype)}{RESET}",
                    file=sys.stderr,
                )
            return "reject"

    return _ask_handler


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


_SLASH_QUIT = object()
_SLASH_HANDLED = object()


@dataclass(frozen=True)
class CommandLine:
    """A line that starts with ``/``, split once for every handler to read.

    ``head`` is lower-cased so commands are case-insensitive; ``args`` keep
    their case, since a reply's text or an ask id is the user's own.
    """

    head: str
    args: tuple[str, ...]

    @classmethod
    def parse(cls, line: str) -> CommandLine | None:
        """The command in *line*, or ``None`` when it is a message."""
        parts = line.split()
        if not parts or not parts[0].startswith("/"):
            return None
        return cls(head=parts[0].lower(), args=tuple(parts[1:]))


async def _handle_ask_command(cmd: CommandLine, remote: Any) -> bool:
    """Handle background-approval slash commands against the daemon (async).

    Returns True if *cmd* was an ask command (and was handled), else False so
    the caller falls through to the normal slash handler / agent turn.
    """
    head, args = cmd.head, cmd.args
    if head == "/asks":
        pending = remote.list_pending()
        if not pending:
            print(f"  {DIM}no pending approvals{RESET}", file=sys.stderr)
        else:
            for a in pending:
                print(f"  {a.get('ask_id', '')[:8]}  {a.get('title', '')}", file=sys.stderr)
        return True
    if head in ("/approve", "/reject"):
        # No id → answer the active (oldest) pending ask. The id is resolved in
        # code so the user never has to type it; an explicit id still works for
        # answering out of order.
        target = args[0] if args else ""
        resp = "approve" if head == "/approve" else "reject"
        print(f"  {await remote.answer(target, resp)}", file=sys.stderr)
        return True
    if head == "/reply":
        if len(args) < 2:
            print(f"  {DIM}usage: /reply <id> <text>{RESET}", file=sys.stderr)
            return True
        print(f"  {await remote.answer(args[0], ' '.join(args[1:]))}", file=sys.stderr)
        return True
    return False


class _RuntimeToggles:
    """The /voice and /subagents settings, loaded on first use.

    Over the same preferences the stack injects every turn — on the
    configured backend, so a ``/voice off`` typed here reaches a daemon on
    Postgres (a SQLite-only store wrote where the daemon never reads,
    ``R-BUG-21``). The store itself belongs to the REPL's context.
    """

    def __init__(self, prefs: PrefsStore) -> None:
        self._prefs = prefs
        self._settings: Any | None = None

    async def get(self) -> Any:
        if self._settings is None:
            self._settings = await RuntimeSettings(self._prefs).load()
        return self._settings

    async def aclose(self) -> None:
        # Let a just-issued write drain before the store goes away (same 50ms
        # courtesy the `yuyutsava prefs` subcommand takes).
        if self._settings is not None:
            await asyncio.sleep(0.05)


def _on_off(word: str) -> bool | None:
    w = word.strip().lower()
    if w in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if w in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    return None


async def _handle_settings_command(cmd: CommandLine, runtime_settings: Any) -> bool:
    """Handle ``/voice`` and ``/subagents`` (async — they hit ``state.db``).

    These write the same ``user_prefs`` rows the daemon and the desktop app
    read, so a toggle typed here reaches every surface without any CLI↔daemon
    transport (the daemon re-reads on a short TTL — see
    :class:`~yuyutsava.prefs.runtime.RuntimeSettings`).

    Returns True when *cmd* was one of these commands (and was handled).
    """
    head = cmd.head
    if head not in ("/voice", "/subagents"):
        return False

    await runtime_settings.refresh(force=True)

    if head == "/voice":
        args = [a.lower() for a in cmd.args]
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
                f"  {DIM}usage: /voice [on|off] | /voice wake on|off | /voice tts on|off{RESET}",
                file=sys.stderr,
            )
            return True
        v = runtime_settings.voice()
        print(f"  {DIM}wake word:{RESET}    {'on' if v.wake_enabled else 'off'}", file=sys.stderr)
        print(f"  {DIM}spoken reply:{RESET} {'on' if v.tts_enabled else 'off'}", file=sys.stderr)
        if not v.wake_enabled:
            print(f"  {DIM}(the mic is still yours to use manually){RESET}", file=sys.stderr)
        return True

    # /subagents [on|off <name>]
    args = cmd.args
    if args:
        if len(args) != 2 or (flag := _on_off(args[0])) is None:
            print(f"  {DIM}usage: /subagents [on|off <name>]{RESET}", file=sys.stderr)
            return True
        name = args[1]
        if name in UNDISABLEABLE and not flag:
            print(
                f"  {DIM}{name} can't be switched off — the master delegates "
                f"to it as a fallback{RESET}",
                file=sys.stderr,
            )
            return True
        await runtime_settings.set_subagent_enabled(name, flag)
    disabled = runtime_settings.subagents().disabled
    if disabled:
        print(f"  {DIM}off:{RESET} {', '.join(sorted(disabled))}", file=sys.stderr)
    else:
        print(f"  {DIM}all dedicated subagents are on{RESET}", file=sys.stderr)
    # This REPL's own roster is just general-purpose (the domain subagents live
    # in the daemon), so say what the toggle actually affects rather than
    # implying a local change that didn't happen.
    print(
        f"  {DIM}applies to the daemon's orchestrator, chat and voice agents{RESET}",
        file=sys.stderr,
    )
    return True


class Outcome(Enum):
    """What the loop should do with a line it handed to the dispatcher."""

    NOT_A_COMMAND = auto()
    HANDLED = auto()
    QUIT = auto()
    #: ``/retry``: run a turn that sends nothing new.
    RETRY = auto()


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
    #: The split-screen dashboard, when the chat runs in one.
    dashboard: Any | None = None

    async def dispatch(self, text: str) -> Outcome:
        line = text.strip()
        cmd = CommandLine.parse(line)
        if self.remote is not None:
            if cmd is not None and await _handle_ask_command(cmd, self.remote):
                return Outcome.HANDLED
            if await self._answered_a_pending_ask(line):
                return Outcome.HANDLED
        if cmd is None:
            return Outcome.NOT_A_COMMAND
        head = cmd.head
        if head in ("/voice", "/subagents"):
            # Async because they read and write state.db, which every other
            # surface reads too. Reached only for these two so a normal message
            # never opens the prefs store.
            await _handle_settings_command(cmd, await self.runtime_toggles.get())
            return Outcome.HANDLED
        if head == "/usage":
            await reports.print_usage_summary(
                self.bundle.usage_store, self.session.thread_id, self.session.created_at
            )
            return Outcome.HANDLED
        if head == "/context":
            reports.print_context(self.session.thread_id, self.console)
            return Outcome.HANDLED
        if head == "/skills":
            reports.print_skills(self.bundle.layout)
            return Outcome.HANDLED
        if head == "/new":
            await self._rotate_session()
            return Outcome.HANDLED
        if head == "/retry":
            return Outcome.RETRY
        result = _handle_slash(
            cmd,
            session_id=self.session.id,
            workspace=self.workspace,
            renderer=self.renderer,
            clear_screen=(
                self.dashboard.clear_transcript if self.dashboard is not None else _clear_terminal
            ),
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
                f"  {DIM}{remaining} more pending — answer with y/n/s/p{RESET}",
                file=sys.stderr,
            )
        return True

    async def _rotate_session(self) -> None:
        """``/new``: a brand-new session row and thread_id, in this process."""
        self.session = await self.convo.new_session(task="(interactive chat)")
        startup.print_banner(
            session_id=self.session.id,
            workspace=self.workspace,
            resuming=False,
            status=self.status_line,
        )


def _clear_terminal() -> None:
    print(CLEAR_SCREEN, end="", file=sys.stderr)


def _handle_slash(
    cmd: CommandLine,
    *,
    session_id: str,
    workspace: Path,
    renderer: "ChatRenderer",
    clear_screen: Callable[[], None] = _clear_terminal,
) -> Any:
    """The commands that need only the terminal: ``_SLASH_QUIT`` to exit,
    otherwise ``_SLASH_HANDLED`` — an unknown command included, after saying so.
    """
    head, args = cmd.head, cmd.args
    if head in ("/quit", "/exit", "/q"):
        return _SLASH_QUIT
    if head == "/help":
        reports.print_help(SLASH_COMMANDS)
        return _SLASH_HANDLED
    if head == "/clear":
        # The terminal's clear, or — in the dashboard, where stderr is the
        # transcript pane and an escape sequence would land in it as text — the
        # pane's own. Then redraw the banner.
        clear_screen()
        startup.print_banner(session_id=session_id, workspace=workspace, resuming=False)
        return _SLASH_HANDLED
    if head == "/session":
        print(f"  {DIM}session:{RESET}   {session_id}", file=sys.stderr)
        print(f"  {DIM}workspace:{RESET} {workspace}", file=sys.stderr)
        return _SLASH_HANDLED
    if head == "/ring":
        renderer.ring.print_all()
        return _SLASH_HANDLED
    if head == "/last":
        k = 1
        if args:
            try:
                k = int(args[0])
            except ValueError:
                print(f"{DIM}/last: expected integer, got {args[0]!r}{RESET}", file=sys.stderr)
                return _SLASH_HANDLED
        renderer.ring.print_last(k)
        return _SLASH_HANDLED
    if head == "/expand":
        if not args:
            print(f"{DIM}/expand: usage: /expand <n>{RESET}", file=sys.stderr)
            return _SLASH_HANDLED
        try:
            idx = int(args[0])
        except ValueError:
            print(f"{DIM}/expand: expected integer, got {args[0]!r}{RESET}", file=sys.stderr)
            return _SLASH_HANDLED
        renderer.ring.print_one(idx)
        return _SLASH_HANDLED
    print(f"{DIM}unknown command: {head} (try /help){RESET}", file=sys.stderr)
    return _SLASH_HANDLED


# ---------------------------------------------------------------------------
# Main REPL entrypoint
# ---------------------------------------------------------------------------


def make_input_reader(dashboard: Any, history_path: Path) -> Callable[[], Awaitable[str]]:
    """Where the REPL's next line comes from, for this terminal.

    The dashboard owns its own input line inside the application layout (same
    history file and completer), so it needs no session here — a
    PromptSession would fight it for the terminal. A TTY gets prompt_toolkit;
    piped input (tests, automation) falls back to plain blocking ``input()``
    in a thread so the REPL still works. Raises ``EOFError`` on Ctrl+D.
    """
    # wrap_lines=True + full-width input area: the input editor spans the
    # whole terminal column count and wraps long lines instead of scrolling
    # horizontally inside a narrow gutter.
    prompt_session: PromptSession[str] | None = (
        PromptSession(
            history=FileHistory(str(history_path)),
            multiline=False,
            wrap_lines=True,
            completer=_SlashCompleter(),
            complete_while_typing=True,
        )
        if sys.stdin.isatty() and dashboard is None
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
                return await prompt_session.prompt_async(ANSI(f"\n{CYAN}>{RESET} "))
        # Non-TTY: run blocking input() in a worker thread.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(f"\n{CYAN}>{RESET} "))

    return _read_input


@dataclass
class ReplLoop:
    """Read a line, run it as a command or as a turn, until told to stop.

    Stops on ``/quit`` (and its aliases), and on Ctrl+D or Ctrl+C at the
    prompt. The session is read from ``commands`` each time, because ``/new``
    replaces it.
    """

    commands: ReplCommands
    convo: Any
    renderer: Any
    ask_handler: Any
    dashboard: Any
    approvals: Any
    usage_store: Any
    read_input: Callable[[], Awaitable[str]]

    async def run(self) -> None:
        while True:
            # Flush any background-task events queued during the previous
            # turn so the user sees them before composing the next message.
            await self.approvals.render_between_turns()
            try:
                user_input = await self.read_input()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return
            user_input = (user_input or "").strip()
            if not user_input:
                continue
            outcome = await self.commands.dispatch(user_input)
            if outcome is Outcome.QUIT:
                return
            if outcome is Outcome.NOT_A_COMMAND:
                await self._turn(user_input)
            elif outcome is Outcome.RETRY:
                await self._turn("", retry=True)

    async def _turn(self, text: str, *, retry: bool = False) -> None:
        started = time.time()
        if not await run_one_turn(
            self.convo,
            text,
            renderer=self.renderer,
            ask_handler=self.ask_handler,
            dashboard=self.dashboard,
            retry=retry,
        ):
            return
        # What the turn cost, from the rows the graph just wrote. The input
        # count climbs with the conversation (the whole history is re-sent
        # every call), and that was invisible from inside a session until now.
        line = format_usage(
            await session_usage(self.usage_store, self.commands.session.thread_id, started)
        )
        if line:
            print(f"{DIM}  {line}{RESET}", file=sys.stderr)


def _install_retry_listener(renderer: Any, dashboard: Any) -> Callable[[], None]:
    """Provider retries (429/503) speak through the renderer, not the log.

    The SDK's own retry logger emits several structlog lines per attempt,
    which printed straight through the Live region and still left the user
    unable to tell whether the turn was alive. The status line is transient
    and easy to miss during a long wait; the panel keeps the notice up for the
    length of the backoff. Neither writes into the transcript. Returns the
    remover, which :func:`_close_chat` calls.
    """

    def _on_retry(model, attempt, retries, delay, exc):
        renderer.note_retry(model, attempt, retries, delay, exc)
        if dashboard is not None:
            dashboard.note_retry(model, attempt, retries, delay, exc)

    return add_retry_listener(_on_retry)


async def _start_session(
    opts: ChatOptions,
    store: Any,
    bundle: Any,
    dashboard: Any,
    sessions_settings: Any,
    *,
    debug_plumbing: bool,
) -> tuple[ConversationService, str]:
    """Resolve the session (--resume / --continue / fresh) and show the banner.

    The terminal is one IO adapter over ``ConversationService`` — the daemon's
    text and voice chats are others. Returns the service and the status line
    the banner showed, which ``/new`` shows again.
    """
    convo, resuming = await ConversationService.resolve(
        store=store,
        bundle=bundle,
        workspace=opts.workspace,
        origin="cli",
        resume_id=opts.resume_id,
        continue_latest=opts.continue_latest,
        agent_path="cli",
        recursion_limit=opts.recursion_limit,
        task="(interactive chat)",
    )
    # The split view must be up BEFORE anything is printed: it takes the
    # alternate screen, which wipes whatever is already on the terminal. Print
    # the banner first and it is erased, leaving `yuyutsava chat` open on a
    # bare screen with a stray Google credentials warning at the top.
    if dashboard is not None:
        await dashboard.start()
    # Surface any langgraph-api upgrade/support notice once, cleanly, right
    # above the banner rather than mid-chat.
    startup.print_version_notice(full=debug_plumbing)
    status_line = startup.startup_status_line(sessions_settings)
    startup.print_banner(
        session_id=convo.session.id,
        workspace=opts.workspace,
        resuming=resuming,
        status=status_line,
    )
    return convo, status_line


async def _close_chat(
    *,
    dashboard: Any,
    renderer: Any,
    approvals: Any,
    runtime_toggles: Any,
    bundle: Any,
    stop_retry_notices: Callable[[], None],
) -> None:
    """Tear the chat down in order, whatever state it ended in."""
    # A renderer that outlived this REPL would keep drawing on a dead Live region.
    stop_retry_notices()
    if dashboard is not None:
        # Leave the alternate screen first, then hand the transcript back: the
        # app takes the session's output with it when it exits, and a user who
        # just spent an hour in here should not find an empty terminal.
        with contextlib.suppress(Exception):
            await dashboard.stop()
        dashboard.replay_to(sys.stderr)
    with contextlib.suppress(Exception):
        await renderer.aclose()
    await approvals.aclose()
    await runtime_toggles.aclose()
    await bundle.aclose()
    # After teardown the docker container is gone, so nothing has the sandbox
    # as its cwd — safe to wipe scratch in either mode.
    if bundle.layout is not None:
        try:
            cleanup_local_sandbox(bundle.layout)
        except Exception:  # noqa: BLE001 — scratch cleanup must not fail the exit
            logger.debug("sandbox scratch cleanup failed", exc_info=True)


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
    if not debug_plumbing:
        debug_plumbing = env_flag("YUYUTSAVA_DEBUG_PLUMBING", true=("1", "true", "yes"))
    if not debug_plumbing:
        silence_plumbing_loggers()

    store = session_store_for()
    sessions_settings = SessionsSettings.from_env()

    # History file lives under the standard YUYUTSAVA state dir so it
    # follows the same lifecycle as the SQLite session store.
    history_path = state_dir() / "chat_history"
    _rotate_history(history_path)

    surface = build_surface(
        workspace, history_path=history_path, verbose=opts.verbose, classic=classic
    )
    console, dashboard, renderer = surface.console, surface.dashboard, surface.renderer
    stop_retry_notices = _install_retry_listener(renderer, dashboard)
    quieten_chat_loggers(debug_plumbing=debug_plumbing, verbose=opts.verbose)

    async with (
        build_checkpointer(sessions_settings) as checkpointer,
        open_events_store() as events,
    ):
        # One preferences store for the per-turn USER PREFERENCES block and
        # the /voice and /subagents toggles.
        prefs = PrefsStore(events)
        # Build the agent stack ONCE. Swallow the LangGraph host's startup
        # banner unless the user asked for the firehose.
        builder = build_agent_stack(
            workspace,
            opts.settings,
            wiring=StackWiring(prefs_store=prefs),
            execution=opts.execution,
            bash_timeout_sec=opts.bash_timeout_sec,
            permission_check=opts.permission_check,
            search_config=opts.search_config,
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

        approvals = await BackgroundApprovals.open(bundle)
        runtime_toggles = _RuntimeToggles(prefs)

        try:
            convo, status_line = await _start_session(
                opts, store, bundle, dashboard, sessions_settings, debug_plumbing=debug_plumbing
            )
            commands = ReplCommands(
                workspace=workspace,
                console=console,
                renderer=renderer,
                bundle=bundle,
                convo=convo,
                runtime_toggles=runtime_toggles,
                status_line=status_line,
                session=convo.session,
                remote=approvals.remote,
                dashboard=dashboard,
            )
            await ReplLoop(
                commands=commands,
                convo=convo,
                renderer=renderer,
                ask_handler=make_ask_handler(renderer, console),
                dashboard=dashboard,
                approvals=approvals,
                usage_store=bundle.usage_store,
                read_input=make_input_reader(dashboard, history_path),
            ).run()

            # Loop exited — flush bookkeeping and mark the session done.
            try:
                await convo.finish("done")
            except Exception:  # noqa: BLE001 — bookkeeping must not fail the exit
                logger.debug("marking the session done failed", exc_info=True)
        finally:
            await _close_chat(
                dashboard=dashboard,
                renderer=renderer,
                approvals=approvals,
                runtime_toggles=runtime_toggles,
                bundle=bundle,
                stop_retry_notices=stop_retry_notices,
            )

    print(f"{DIM}— chat closed —{RESET}", file=sys.stderr)
    return 0
