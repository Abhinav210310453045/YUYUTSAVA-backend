"""Crash-safe wrapper around ``astream_agent`` for the CLI.

Owns the session-row lifecycle so a SIGKILL, Ctrl+C, terminal close, or power
loss still leaves a recoverable row in the store:

    1. ``store.create`` BEFORE any LLM call — durable from tick zero.
    2. Streaming ticks call ``store.touch`` (coalesced) to bump ``updated_at``
       and message counters.
    3. ``finally:`` flips status to ``done`` (graceful) or ``crashed``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from langchain_core.messages import ToolMessage
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.core.engine import astream_agent
from yuyutsava.core.interrupts_store import InterruptsStore
from yuyutsava.sessions.config import SessionsSettings
from yuyutsava.sessions.models import Session
from yuyutsava.sessions.store import SessionNotFound, SessionStore


_TICK_COALESCE_SEC = 0.5
_BANNER = "═" * 60


class ResumeFailed(Exception):
    """``--resume <id>`` was given but no matching session exists.

    Surfaced to the CLI so it can print a clean message and exit non-zero
    without a Python traceback.
    """


_CANCELLED_TOOL_MARKER = "was cancelled - another message came in"
_DENIED_REPLACEMENT = (
    "DENIED: the user did not approve this action — the previous session was "
    "interrupted (Ctrl+C, terminal close, crash) before the permission prompt "
    "could be answered. If this action is still required, re-propose it "
    "explicitly so the user can decide. Do NOT assume it succeeded."
)


async def _patch_orphan_cancellations(agent: CompiledStateGraph, thread_id: str) -> int:
    """Rewrite ``cancelled`` ToolMessages left over from a killed permission prompt.

    LangGraph fabricates a ToolMessage with ``status='success'`` and content
    "Tool call ... was cancelled - another message came in before it could be
    completed." whenever a tool task is cancelled mid-flight. That wording +
    success status causes the model on resume to believe the call succeeded,
    leading to hallucinations like "I wrote the file" when nothing was written.

    Rewrite those messages (preserving id so the LangGraph ``add_messages``
    reducer merges them in place) with ``status='error'`` and an explicit
    denial — the model already handles denials correctly today.

    Returns the number of messages patched.
    """
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = await agent.aget_state(config)
    except Exception:
        return 0
    msgs = state.values.get("messages", []) if state and state.values else []
    patched: list = []
    for m in msgs:
        content = m.content if isinstance(m.content, str) else ""
        if type(m).__name__ == "ToolMessage" and _CANCELLED_TOOL_MARKER in content:
            patched.append(ToolMessage(
                id=m.id,
                tool_call_id=m.tool_call_id,
                name=getattr(m, "name", "tool") or "tool",
                status="error",
                content=_DENIED_REPLACEMENT,
            ))
    if not patched:
        return 0
    try:
        await agent.aupdate_state(config, {"messages": patched})
    except Exception as exc:
        print(f"\033[33msessions:\033[0m could not patch orphan tool calls: {exc}",
              file=sys.stderr)
        return 0
    return len(patched)


def _count_memory_files(workspace: Path) -> int:
    """Count workspace-scoped skill files. Cheap, defensive against missing dirs.

    Matches the convention used by ``SkillRegistry`` — workspace skills live in
    ``<workspace>/.skills/<slug>/SKILL.md``.
    """
    skills_dir = workspace / ".skills"
    if not skills_dir.is_dir():
        return 0
    try:
        return sum(1 for _ in skills_dir.rglob("SKILL.md"))
    except OSError:
        return 0


def _print_start_banner(session: Session, *, resuming: bool) -> None:
    verb = "resuming" if resuming else "starting"
    cmd = (
        f"uv run yuyutsava --verbose --workspace {session.workspace} "
        f"--resume {session.id}"
    )
    print(f"\n\033[36m{_BANNER}\033[0m", file=sys.stderr)
    print(f"\033[36mYUYUTSAVA — {verb} session\033[0m", file=sys.stderr)
    print(f"  session_id : {session.id}", file=sys.stderr)
    print(f"  workspace  : {session.workspace}", file=sys.stderr)
    print(f"  resume cmd : {cmd}", file=sys.stderr)
    print(f"\033[36m{_BANNER}\033[0m\n", file=sys.stderr)


class _CoalescedTicker:
    """Batch ``store.touch`` calls so a chatty agent doesn't thrash WAL."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._pending_delta = 0
        self._last_flush = 0.0
        self._lock = asyncio.Lock()

    async def tick(self, delta: int = 1) -> None:
        self._pending_delta += delta
        now = time.monotonic()
        if now - self._last_flush < _TICK_COALESCE_SEC:
            return
        await self._flush(now)

    async def flush_final(self, *, memory_files_count: int) -> None:
        # Force-write any pending counts plus the memory snapshot.
        async with self._lock:
            await self._store.touch(
                self._session_id,
                message_delta=self._pending_delta,
                memory_files_count=memory_files_count,
            )
            self._pending_delta = 0
            self._last_flush = time.monotonic()

    async def _flush(self, now: float) -> None:
        async with self._lock:
            if self._pending_delta == 0:
                return
            delta, self._pending_delta = self._pending_delta, 0
            self._last_flush = now
        await self._store.touch(self._session_id, message_delta=delta)


async def _resolve_session(
    store: SessionStore,
    *,
    workspace: Path,
    task: str,
    resume_id: str | None,
    continue_latest: bool,
) -> tuple[Session, bool]:
    """Return ``(session, resuming)``. Raises if --resume id is unknown.

    ``--continue`` with no prior session in this workspace falls through to a
    fresh row — the user explicitly asked to continue, so the new session
    inherits the workspace context.
    """
    if resume_id:
        try:
            existing = await store.get(resume_id)
        except SessionNotFound:
            raise ResumeFailed(
                f"No session with id {resume_id!r}. "
                f"Run with --list-sessions to see available ids."
            ) from None
        if str(existing.workspace) != str(workspace.resolve()):
            print(
                f"\033[33mwarning:\033[0m resuming session {resume_id} "
                f"whose original workspace was {existing.workspace} "
                f"(current --workspace is {workspace}). Continuing anyway.",
                file=sys.stderr,
            )
        await store.update_status(resume_id, "running")
        return existing, True

    if continue_latest:
        rows = await store.list(workspace=workspace, limit=1)
        if rows:
            await store.update_status(rows[0].id, "running")
            return rows[0], True

    fresh = await store.create(workspace=workspace, task=task)
    return fresh, False


async def run_session(
    *,
    store: SessionStore,
    agent: CompiledStateGraph,
    task: str,
    workspace: Path,
    resume_id: str | None = None,
    continue_latest: bool = False,
    recursion_limit: int = 200,
    agent_path: str = "cli",
    interrupts_store: InterruptsStore | None = None,
) -> str:
    """Persist + run + bookkeep one CLI session.

    Returns the final assistant text (or ``""`` if the agent produced none).
    Re-raises whatever ``astream_agent`` raised after marking the row crashed.
    """
    session, resuming = await _resolve_session(
        store, workspace=workspace, task=task,
        resume_id=resume_id, continue_latest=continue_latest,
    )
    _print_start_banner(session, resuming=resuming)

    # Lazily construct the interrupt audit store from env config when the
    # caller didn't pass one in. Best-effort: if it fails to open we still run.
    if interrupts_store is None:
        try:
            settings = SessionsSettings.from_env()
            if settings.interrupts_db_path is not None:
                interrupts_store = InterruptsStore(
                    settings.interrupts_db_path,
                    busy_timeout_ms=settings.busy_timeout_ms,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"\033[33msessions:\033[0m interrupts store init failed: {exc}",
                  file=sys.stderr)
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
                print(f"\033[33msessions:\033[0m mark_orphaned failed: {exc}",
                      file=sys.stderr)
        # Refresh task_preview so the sessions list shows the most recent intent
        # rather than the original prompt from the first run.
        new_preview = (task or "").strip().replace("\n", " ")
        if new_preview:
            await store.touch(session.id, task_preview=new_preview)

    ticker = _CoalescedTicker(store, session.id)

    async def _on_tick(steps: int) -> None:
        await ticker.tick(steps)

    completed = False
    try:
        final = await astream_agent(
            agent, task,
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
            await ticker.flush_final(memory_files_count=_count_memory_files(workspace))
            await store.update_status(
                session.id, "done" if completed else "crashed",
            )
        except Exception as exc:  # noqa: BLE001
            # Never let bookkeeping mask the original failure.
            print(f"\033[33msessions:\033[0m bookkeeping failed: {exc}",
                  file=sys.stderr)
