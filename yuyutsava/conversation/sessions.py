"""The session row behind a conversation: creating it, resuming it, keeping it current.

A turn has to survive the process running it. The row is written before the
first model call, so a SIGKILL, a closed terminal or a power cut still leaves a
``running`` row that ``--resume`` can pick up; ticks bump its counters while the
agent works (coalesced, so a chatty agent doesn't thrash the WAL), and the
caller flips it to ``done`` or ``crashed`` at the end.

Both fronts share this. :class:`~yuyutsava.conversation.service.ConversationService`
runs the REPL, the app and voice; :mod:`yuyutsava.cli.session_run` runs the
one-shot command. These four names lived in a ``sessions/`` package the CLI
owned, which left the service importing private names out of a layer above the
one it belongs to, and the name collided with :mod:`yuyutsava.storage.sessions`
(the row's storage, which is still where the store itself lives). R-PLC-06.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from yuyutsava.ansi import RESET, YELLOW
from yuyutsava.storage.models import Session
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.storage.sessions import SessionNotFound, SessionStore

_TICK_COALESCE_SEC = 0.5


class ResumeFailed(Exception):
    """``--resume <id>`` was given but no matching session exists.

    Surfaced to the CLI so it can print a clean message and exit non-zero
    without a Python traceback.
    """


def count_memory_files(workspace: Path) -> int:
    """Count workspace-scoped skill files. Cheap, defensive against missing dirs.

    Matches the convention used by ``SkillRegistry`` — workspace skills live in
    ``<workspace>/.yuyutsava/skills/<slug>/SKILL.md`` (``WorkspaceLayout.skills``).
    """
    skills_dir = WorkspaceLayout.for_workspace(workspace).skills
    if not skills_dir.is_dir():
        return 0
    try:
        return sum(1 for _ in skills_dir.rglob("SKILL.md"))
    except OSError:
        return 0


class CoalescedTicker:
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


async def resolve_session(
    store: SessionStore,
    *,
    workspace: Path,
    task: str,
    resume_id: str | None,
    continue_latest: bool,
    origin: str = "cli",
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
                f"No session with id {resume_id!r}. Run with --list-sessions to see available ids."
            ) from None
        if str(existing.workspace) != str(workspace.resolve()):
            print(
                f"{YELLOW}warning:{RESET} resuming session {resume_id} "
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

    fresh = await store.create(workspace=workspace, task=task, origin=origin)
    return fresh, False
