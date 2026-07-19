"""
Thin async filesystem and shell executor.

Called exclusively AFTER permission has been granted.
No permission logic lives here — only I/O.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

import httpx

from yuyutsava.platform.process import run_capture


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
    *,
    elevated: bool = False,
) -> dict:
    """
    Run *command* in this host's native shell within *cwd*.

    The shell is chosen by the HostProfile — ``bash -c`` on POSIX,
    ``powershell -Command`` on Windows (never ``cmd.exe``) — so the model's
    OS-native command syntax runs correctly everywhere. We use
    ``create_subprocess_exec`` with an explicit argv (not ``_shell``) so there
    is no second layer of shell-quoting to get wrong.

    When *elevated* is True the command is routed through the platform
    elevation provider (UAC / admin osascript / pkexec) instead. The caller
    MUST have taken fresh CRITICAL consent first — this layer only executes.

    Returns a dict with keys: ``stdout``, ``stderr``, ``exit_code``.
    Raises ``asyncio.TimeoutError`` if the command exceeds *timeout* seconds.
    """
    cwd.mkdir(parents=True, exist_ok=True)

    if elevated:
        from yuyutsava.platform.elevation import get_elevation_provider

        res = await get_elevation_provider().run_elevated(command, timeout=timeout)
        return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code}

    from yuyutsava.platform import host_profile

    argv = host_profile().shell_command(command)
    # run_capture is loop-agnostic: on Windows the daemon runs on a Selector
    # loop (psycopg) that cannot create_subprocess_exec, so it spawns in a
    # worker thread; on POSIX it uses the native asyncio subprocess path.
    stdout_bytes, stderr_bytes, exit_code = await run_capture(
        argv, cwd=str(cwd), timeout=timeout
    )

    return {
        "stdout": stdout_bytes.decode(errors="replace").strip(),
        "stderr": stderr_bytes.decode(errors="replace").strip(),
        "exit_code": exit_code,
    }


async def execute_grep(
    pattern: str,
    path: Path,
    *,
    context_lines: int = 3,
    case_insensitive: bool = False,
    max_matches: int = 100,
) -> dict:
    """Pure-Python recursive regex search — replaces shelling out to ``grep -rn``.

    Returns a ShellResult-shaped dict (``stdout``/``stderr``/``exit_code``) so
    the tool contract is unchanged. ``stdout`` lines are ``relpath:lineno:text``
    for matches and ``relpath-lineno-text`` for context (mirrors ``grep -n``),
    so returned line numbers still feed ``tr_read_file`` offsets. ``exit_code``
    follows grep: 0 = matches found, 1 = none, 2 = error.
    """
    return await asyncio.to_thread(
        _sync_grep, pattern, path, context_lines, case_insensitive, max_matches
    )


async def execute_fetch(
    url: str,
    dest: Path,
    *,
    user_agent: str,
    timeout: int = 120,
) -> dict:
    """Download *url* to *dest* with httpx — replaces shelling out to ``curl -fSL``.

    Follows redirects, sends a browser User-Agent, streams to disk, retries a
    couple of times on transport errors. Returns a ShellResult-shaped dict so
    ``tr_fetch_url``'s existing verification (which reads ``exit_code`` and
    ``stderr``) works unchanged. ``exit_code`` mirrors curl: 0 ok, 22 HTTP
    error, 7 connect/transport failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            last_err = "unreachable"
            for attempt in (1, 2, 3):
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code >= 400:
                            last_err = f"HTTP {resp.status_code}"
                            if attempt < 3 and resp.status_code >= 500:
                                continue
                            return {"stdout": "", "stderr": last_err, "exit_code": 22}
                        with dest.open("wb") as fh:
                            async for chunk in resp.aiter_bytes():
                                fh.write(chunk)
                        return {"stdout": str(dest), "stderr": "", "exit_code": 0}
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_err = str(exc)
                    if attempt == 3:
                        return {"stdout": "", "stderr": last_err, "exit_code": 7}
            return {"stdout": "", "stderr": last_err, "exit_code": 7}
    except Exception as exc:  # noqa: BLE001 — surface any client-construction failure
        return {"stdout": "", "stderr": str(exc), "exit_code": 1}


async def execute_python(script_path: Path, cwd: Path, timeout: int = 120) -> dict:
    """Run a ``.py`` file with THIS interpreter (``sys.executable``) — portable.

    Uses ``create_subprocess_exec`` with an explicit argv (script path as a
    single arg), so there is ZERO shell quoting/escaping to get wrong on any
    OS. This is the portable-scripting primitive behind ``tr_run_python``: the
    model writes Python (identical on Windows/macOS/Linux) instead of a bash
    script.

    Returns a dict with keys ``stdout``, ``stderr``, ``exit_code``.
    Raises ``asyncio.TimeoutError`` if the script exceeds *timeout* seconds.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    out, err, exit_code = await run_capture(
        [sys.executable, str(script_path)], cwd=str(cwd), timeout=timeout
    )
    return {
        "stdout": out.decode(errors="replace").strip(),
        "stderr": err.decode(errors="replace").strip(),
        "exit_code": exit_code,
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


# Dirs never worth scanning + a per-file size cap. Skipping these (not being
# written in C) is what keeps the pure-Python search fast on real trees.
_GREP_IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".tox", ".ruff_cache", "dist", "build", ".idea", ".DS_Store",
})
_GREP_MAX_FILE_BYTES = 5 * 1024 * 1024


def _sync_grep(
    pattern: str,
    path: Path,
    context_lines: int,
    case_insensitive: bool,
    max_matches: int,
) -> dict:
    try:
        rx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return {"stdout": "", "stderr": f"invalid regex: {exc}", "exit_code": 2}

    if path.is_file():
        files: list[Path] = [path]
        base = path.parent
    elif path.is_dir():
        files = []
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _GREP_IGNORE_DIRS]
            files.extend(Path(root) / n for n in names)
        base = path
    else:
        return {"stdout": "", "stderr": f"path not found: {path}", "exit_code": 2}

    out: list[str] = []
    count = 0
    for fp in files:
        if count >= max_matches:
            break
        try:
            if fp.stat().st_size > _GREP_MAX_FILE_BYTES:
                continue
            raw = fp.read_bytes()
            if b"\x00" in raw[:8192]:  # binary sniff — skip
                continue
            lines = raw.decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        rel = os.path.relpath(fp, base)
        for i, line in enumerate(lines):
            if count >= max_matches:
                break
            if rx.search(line):
                lo = max(0, i - context_lines)
                hi = min(len(lines), i + context_lines + 1)
                for j in range(lo, hi):
                    sep = ":" if j == i else "-"
                    out.append(f"{rel}{sep}{j + 1}{sep}{lines[j]}")
                out.append("--")
                count += 1

    while out and out[-1] == "--":
        out.pop()
    return {"stdout": "\n".join(out), "stderr": "", "exit_code": 0 if count else 1}
