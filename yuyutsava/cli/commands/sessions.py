"""``--list-sessions`` and ``--delete-session`` handlers.

Pure read/write against the sessions store + checkpointer; no model, no Docker.
These short-circuit early in cli.py before any heavy agent construction.
"""

from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

from yuyutsava.storage.sessions import (
    SessionNotFound,
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
)


_STATUS_COLOR_CODES = {
    "running": "\033[32m",
    "done":    "\033[2m",
    "crashed": "\033[31m",
    "idle":    "\033[33m",
}


def _human_bytes(n: int) -> str:
    """Format ``n`` bytes as KB/MB/GB for the sessions table."""
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}PB"


def _human_age(now: float, then: float) -> str:
    """Compact "3m ago" / "2h ago" / "5d ago" for the sessions table."""
    delta = max(0.0, now - then)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _ansi(code: str) -> str:
    """Return an ANSI escape only when stdout is a real TTY.

    Keeps the table copy-paste-friendly when piped through ``less``, ``grep``,
    or redirected to a file.
    """
    return code if sys.stdout.isatty() else ""


async def print_sessions_table(workspace_filter: Path | None = None) -> int:
    """``--list-sessions`` handler. Prints to stdout, returns process exit code.

    Renders each row as a labelled card with a fully-formed copy-paste resume
    command beneath it. Long fields (workspace, task) are NOT truncated — the
    point of this view is to be the source of truth the user copies from.
    """
    store = get_default_session_store()
    rows = await store.list(workspace=workspace_filter, limit=100)
    if not rows:
        print("(no sessions yet — start one with `uv run yuyutsava <task>`)")
        return 0

    reset  = _ansi("\033[0m")
    dim    = _ansi("\033[2m")
    bold   = _ansi("\033[1m")
    cyan   = _ansi("\033[36m")
    yellow = _ansi("\033[33m")

    now = time.time()
    scope = "this workspace" if workspace_filter is not None else "all workspaces"
    sep = "─" * 78
    print(f"{bold}Sessions ({len(rows)}) — {scope}{reset}")
    print(dim + sep + reset)

    for s in rows:
        status_colour = _ansi(_STATUS_COLOR_CODES.get(s.status, ""))
        status_str = f"{status_colour}{s.status}{reset}"
        ws_quoted = shlex.quote(str(s.workspace))
        resume_cmd = (
            f"uv run yuyutsava --verbose --workspace {ws_quoted} "
            f"--resume {s.id} \"<your next message>\""
        )

        print(f"{bold}{cyan}{s.id}{reset}")
        print(f"  {dim}status   {reset}{status_str}"
              f"   {dim}updated  {reset}{_human_age(now, s.updated_at)}"
              f"   {dim}msgs  {reset}{s.message_count}"
              f"   {dim}mem  {reset}{s.memory_files_count}"
              f"   {dim}size  {reset}{_human_bytes(s.db_row_bytes)}")
        print(f"  {dim}workspace{reset}  {s.workspace}")
        print(f"  {dim}task     {reset} {s.task_preview}")
        print(f"  {dim}resume   {reset} {yellow}{resume_cmd}{reset}")
        print(dim + sep + reset)

    print(f"{dim}tip:{reset} replace {yellow}\"<your next message>\"{reset} "
          f"with what you want the agent to do next, then paste into a terminal.")
    return 0


async def delete_session(session_id: str) -> int:
    """``--delete-session`` handler. Removes the session row AND its
    checkpoint rows. Prints a one-line confirmation or error.
    """
    store = get_default_session_store()
    try:
        s = await store.get(session_id)
    except SessionNotFound:
        print(f"Error: no session with id {session_id!r}", file=sys.stderr)
        return 2
    settings = SessionsSettings.from_env()
    async with build_checkpointer(settings) as saver:
        await saver.adelete_thread(s.thread_id)
    await store.delete(session_id)
    print(f"Deleted session {session_id} (workspace: {s.workspace})")
    return 0
