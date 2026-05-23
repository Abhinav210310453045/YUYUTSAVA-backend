"""
Thin async filesystem and shell executor.

Called exclusively AFTER permission has been granted.
No permission logic lives here — only I/O.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def execute_read(
    path: Path,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Read *path* and return a dict with content and pagination metadata.

    Args:
        path:   File to read.
        offset: 0-based line number to start from (default 0 = beginning).
        limit:  Maximum number of lines to return.  None = read to EOF.

    Returns a dict with keys:
        content        — the requested text slice (joined lines)
        total_lines    — total lines in the full file
        offset         — echoed back from the request
        returned_lines — number of lines actually returned
        has_more       — True when there are lines after this chunk
    """
    return await asyncio.to_thread(_sync_read_paginated, path, offset, limit)


async def execute_write(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories as needed."""
    await asyncio.to_thread(_sync_write, path, content)


async def execute_delete(path: Path) -> None:
    """Delete *path* (file or directory tree)."""
    await asyncio.to_thread(_sync_delete, path)


async def execute_list(path: Path, max_entries: int = 500) -> dict:
    """List entries in *path* (real filesystem).

    Returns a dict with keys:
        entries  — list of {name, path, type, size} (capped at max_entries)
        total    — total entry count before capping
        has_more — True when entries were truncated
    """
    return await asyncio.to_thread(_sync_list, path, max_entries)


async def execute_glob(
    root: Path,
    pattern: str,
    max_entries: int = 500,
) -> dict:
    """Glob *pattern* under *root* (real filesystem).

    Uses ``Path.rglob`` for recursive patterns (``**``) and ``Path.glob``
    for shallow ones. Returns same shape as ``execute_list``.
    """
    return await asyncio.to_thread(_sync_glob, root, pattern, max_entries)


async def execute_run(
    command: str,
    cwd: Path,
    timeout: int = 120,
) -> dict:
    """
    Run *command* in a subprocess within *cwd*.

    Returns a dict with keys: ``stdout``, ``stderr``, ``exit_code``.
    Raises ``asyncio.TimeoutError`` if the command exceeds *timeout* seconds.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    return {
        "stdout": stdout_bytes.decode(errors="replace").strip(),
        "stderr": stderr_bytes.decode(errors="replace").strip(),
        "exit_code": proc.returncode,
    }


# ---------------------------------------------------------------------------
# Sync helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _sync_read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _sync_read_paginated(
    path: Path,
    offset: int,
    limit: int | None,
) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines(keepends=True)
    total_lines = len(all_lines)

    start = max(0, offset)
    end = total_lines if limit is None else min(start + limit, total_lines)
    slice_lines = all_lines[start:end]
    returned_lines = len(slice_lines)

    return {
        "content": "".join(slice_lines),
        "total_lines": total_lines,
        "offset": start,
        "returned_lines": returned_lines,
        "has_more": (start + returned_lines) < total_lines,
    }


def _sync_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sync_delete(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _entry_dict(p: Path) -> dict:
    try:
        if p.is_symlink():
            kind, size = "symlink", None
        elif p.is_dir():
            kind, size = "dir", None
        elif p.is_file():
            kind, size = "file", p.stat().st_size
        else:
            kind, size = "other", None
    except OSError:
        kind, size = "other", None
    return {"name": p.name, "path": str(p), "type": kind, "size": size}


def _sync_list(path: Path, max_entries: int) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {path}")
    all_entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    total = len(all_entries)
    sliced = all_entries[:max_entries]
    return {
        "entries": [_entry_dict(p) for p in sliced],
        "total": total,
        "has_more": total > max_entries,
    }


def _sync_glob(root: Path, pattern: str, max_entries: int) -> dict:
    if not root.exists():
        raise FileNotFoundError(f"Root path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root}")
    iterator = root.rglob(pattern) if "**" in pattern else root.glob(pattern)
    collected: list[Path] = []
    total = 0
    for p in iterator:
        total += 1
        if len(collected) < max_entries:
            collected.append(p)
    collected.sort(key=lambda p: (not p.is_dir(), str(p).lower()))
    return {
        "entries": [_entry_dict(p) for p in collected],
        "total": total,
        "has_more": total > max_entries,
    }
