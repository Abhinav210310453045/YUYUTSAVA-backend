"""The one-shot CLI turn: ``yuyutsava "<task>"``, run once and report.

One agent run wrapped in the session-row lifecycle from
:mod:`yuyutsava.conversation.sessions`, plus the banner that tells the user the
exact command to resume it. On resume it first repairs tool calls the previous
process left cancelled: a permission prompt dies with its terminal, and the
agent coming back would otherwise read the cancelled result as success.

Everything here is the CLI's own surface — it prints to stderr and owns no
policy anyone else needs. Like the multi-turn fronts (the REPL, the app, voice),
a turn here first repairs a history the model would not continue from and never
ends in silence: both come from :mod:`yuyutsava.conversation.recovery`, the
same functions :class:`~yuyutsava.conversation.service.ConversationService`
uses (R-BUG-06).
"""

from __future__ import annotations

import sys
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from yuyutsava.ansi import CYAN, DIM, RED, RESET, YELLOW
from yuyutsava.cli.render.terminal_stream import astream_agent
from yuyutsava.conversation.recovery import recover_empty_turn, repair_before_turn
from yuyutsava.conversation.repair import Cause, repair_orphan_tool_calls
from yuyutsava.conversation.sessions import (
    CoalescedTicker,
    count_memory_files,
    resolve_session,
)
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.storage.interrupts import InterruptsStore
from yuyutsava.storage.interrupts_unified import sqlite_interrupts_store
from yuyutsava.storage.models import Session
from yuyutsava.storage.sessions import SessionsSettings, SessionStore

_BANNER = "═" * 60


async def _patch_orphan_cancellations(agent: CompiledStateGraph, thread_id: str) -> int:
    """Repair cancellation results left by a killed permission prompt.

    Thin wrapper kept for this call site's readability; the logic and the
    reasoning now live in :mod:`yuyutsava.conversation.repair`, because the
    daemon needs exactly the same repair and previously had none — which is
    how an app thread could stay wedged forever.

    ``SESSION_ENDED`` because this runs on resume: the prompt died with the
    previous process, so consent was never given.
    """
    return await repair_orphan_tool_calls(agent, thread_id, cause=Cause.SESSION_ENDED)


def _print_notice(ev: StreamEvent) -> None:
    """A recovery notice, in the terminal's own voice."""
    level = ev.data.get("level", "info")
    colour = {"error": RED, "warning": YELLOW}.get(level, DIM)
    mark = {"error": "✗", "warning": "⚠"}.get(level, "·")
    print(f"  {colour}{mark} {ev.data.get('text', '')}{RESET}", file=sys.stderr)


def _print_start_banner(session: Session, *, resuming: bool) -> None:
    verb = "resuming" if resuming else "starting"
    cmd = f"uv run yuyutsava --verbose --workspace {session.workspace} --resume {session.id}"
    print(f"\n{CYAN}{_BANNER}{RESET}", file=sys.stderr)
    print(f"{CYAN}YUYUTSAVA — {verb} session{RESET}", file=sys.stderr)
    print(f"  session_id : {session.id}", file=sys.stderr)
    print(f"  workspace  : {session.workspace}", file=sys.stderr)
    print(f"  resume cmd : {cmd}", file=sys.stderr)
    print(f"{CYAN}{_BANNER}{RESET}\n", file=sys.stderr)


async def run_session(
    *,
    store: SessionStore,
    agent: CompiledStateGraph,
    task: str,
    workspace: Path,
    resume_id: str | None = None,
    continue_latest: bool = False,
    recursion_limit: int = 1000,
    agent_path: str = "cli",
    origin: str = "cli",
    interrupts_store: InterruptsStore | None = None,
) -> str:
    """Persist + run + bookkeep one CLI session.

    Returns the final assistant text (or ``""`` if the agent produced none).
    Re-raises whatever ``astream_agent`` raised after marking the row crashed.
    """
    session, resuming = await resolve_session(
        store,
        workspace=workspace,
        task=task,
        resume_id=resume_id,
        continue_latest=continue_latest,
        origin=origin,
    )
    _print_start_banner(session, resuming=resuming)

    # Lazily construct the interrupt audit store from env config when the
    # caller didn't pass one in. Best-effort: if it fails to open we still run.
    if interrupts_store is None:
        try:
            settings = SessionsSettings.from_env()
            if settings.interrupts_db_path is not None:
                interrupts_store = sqlite_interrupts_store(
                    settings.interrupts_db_path,
                    busy_timeout_ms=settings.busy_timeout_ms,
                )
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not stop the turn
            print(f"{YELLOW}sessions:{RESET} interrupts store init failed: {exc}", file=sys.stderr)
            interrupts_store = None

    if resuming:
        n_patched = await _patch_orphan_cancellations(agent, session.thread_id)
        if n_patched:
            print(
                f"{YELLOW}note:{RESET} rewrote {n_patched} cancelled tool call(s) "
                f"from the previous interrupt as DENIED so the agent doesn't "
                f"hallucinate success. Re-ask if you wanted that action to run.",
                file=sys.stderr,
            )
        if interrupts_store is not None:
            try:
                n_orphaned = await interrupts_store.mark_orphaned_for_session(session.id)
                if n_orphaned:
                    print(
                        f"{DIM}sessions:{RESET} flagged {n_orphaned} unresolved "
                        f"interrupt(s) from prior run as orphaned.",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001 — bookkeeping must not stop the turn
                print(f"{YELLOW}sessions:{RESET} mark_orphaned failed: {exc}", file=sys.stderr)
        # Refresh task_preview so the sessions list shows the most recent intent
        # rather than the original prompt from the first run.
        new_preview = (task or "").strip().replace("\n", " ")
        if new_preview:
            await store.touch(session.id, task_preview=new_preview)

    ticker = CoalescedTicker(store, session.id)

    async def _on_tick(steps: int) -> None:
        await ticker.tick(steps)

    async def _drive(user_text: str | None, _resume: object | None) -> tuple[str, int]:
        final_text = await astream_agent(
            agent,
            user_text,
            thread_id=session.thread_id,
            recursion_limit=recursion_limit,
            on_tick=_on_tick,
            agent_path=agent_path,
            session_id=session.id,
            interrupts_store=interrupts_store,
            invocation_mode="cli",
        )
        return final_text, 1

    completed = False
    try:
        # No turn starts from a history the model will not continue from — the
        # same guarantee every other front gets from ConversationService.
        await repair_before_turn(agent, session.thread_id, _print_notice)
        final, _ = await _drive(task, None)
        if not final:
            final, _ = await recover_empty_turn(agent, session.thread_id, _print_notice, _drive)
        completed = True
        return final
    finally:
        try:
            await ticker.flush_final(memory_files_count=count_memory_files(workspace))
            await store.update_status(
                session.id,
                "done" if completed else "crashed",
            )
        except Exception as exc:  # noqa: BLE001 — must not mask the turn's failure
            # Never let bookkeeping mask the original failure.
            print(f"{YELLOW}sessions:{RESET} bookkeeping failed: {exc}", file=sys.stderr)
