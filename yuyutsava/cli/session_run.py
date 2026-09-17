"""The one-shot CLI turn: ``yuyutsava "<task>"``, run once and report.

One agent run wrapped in the session-row lifecycle from
:mod:`yuyutsava.conversation.sessions`, plus the banner that tells the user the
exact command to resume it. On resume it first repairs tool calls the previous
process left cancelled: a permission prompt dies with its terminal, and the
agent coming back would otherwise read the cancelled result as success.

Everything here is the CLI's own surface — it prints to stderr and owns no
policy anyone else needs. The multi-turn fronts (the REPL, the app, voice) run
:class:`~yuyutsava.conversation.service.ConversationService`, which repairs
interrupted turns and retries empty ones; this path does neither, so W5 of the
code review moves it onto the service as a behaviour change of its own
(R-PLC-06).
"""

from __future__ import annotations

import sys
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from yuyutsava.cli.render.terminal_stream import astream_agent
from yuyutsava.conversation.repair import Cause, repair_orphan_tool_calls
from yuyutsava.conversation.sessions import (
    CoalescedTicker,
    count_memory_files,
    resolve_session,
)
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


def _print_start_banner(session: Session, *, resuming: bool) -> None:
    verb = "resuming" if resuming else "starting"
    cmd = f"uv run yuyutsava --verbose --workspace {session.workspace} --resume {session.id}"
    print(f"\n\033[36m{_BANNER}\033[0m", file=sys.stderr)
    print(f"\033[36mYUYUTSAVA — {verb} session\033[0m", file=sys.stderr)
    print(f"  session_id : {session.id}", file=sys.stderr)
    print(f"  workspace  : {session.workspace}", file=sys.stderr)
    print(f"  resume cmd : {cmd}", file=sys.stderr)
    print(f"\033[36m{_BANNER}\033[0m\n", file=sys.stderr)


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
        except Exception as exc:  # noqa: BLE001
            print(f"\033[33msessions:\033[0m interrupts store init failed: {exc}", file=sys.stderr)
            interrupts_store = None

    if resuming:
        n_patched = await _patch_orphan_cancellations(agent, session.thread_id)
        if n_patched:
            print(
                f"\033[33mnote:\033[0m rewrote {n_patched} cancelled tool call(s) "
                f"from the previous interrupt as DENIED so the agent doesn't "
                f"hallucinate success. Re-ask if you wanted that action to run.",
                file=sys.stderr,
            )
        if interrupts_store is not None:
            try:
                n_orphaned = await interrupts_store.mark_orphaned_for_session(session.id)
                if n_orphaned:
                    print(
                        f"\033[2msessions:\033[0m flagged {n_orphaned} unresolved "
                        f"interrupt(s) from prior run as orphaned.",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"\033[33msessions:\033[0m mark_orphaned failed: {exc}", file=sys.stderr)
        # Refresh task_preview so the sessions list shows the most recent intent
        # rather than the original prompt from the first run.
        new_preview = (task or "").strip().replace("\n", " ")
        if new_preview:
            await store.touch(session.id, task_preview=new_preview)

    ticker = CoalescedTicker(store, session.id)

    async def _on_tick(steps: int) -> None:
        await ticker.tick(steps)

    completed = False
    try:
        final = await astream_agent(
            agent,
            task,
            thread_id=session.thread_id,
            recursion_limit=recursion_limit,
            on_tick=_on_tick,
            agent_path=agent_path,
            session_id=session.id,
            interrupts_store=interrupts_store,
            invocation_mode="cli",
        )
        completed = True
        return final
    finally:
        try:
            await ticker.flush_final(memory_files_count=count_memory_files(workspace))
            await store.update_status(
                session.id,
                "done" if completed else "crashed",
            )
        except Exception as exc:  # noqa: BLE001
            # Never let bookkeeping mask the original failure.
            print(f"\033[33msessions:\033[0m bookkeeping failed: {exc}", file=sys.stderr)
