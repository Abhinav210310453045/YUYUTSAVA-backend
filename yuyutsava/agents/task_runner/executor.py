"""
Thin async filesystem and shell executor.

Called exclusively AFTER permission has been granted.
No permission logic lives here — only I/O.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def execute_read(path: Path) -> str:
    """Read and return the text content of *path*."""
    return await asyncio.to_thread(_sync_read, path)


async def execute_write(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories as needed."""
    await asyncio.to_thread(_sync_write, path, content)


async def execute_delete(path: Path) -> None:
    """Delete *path* (file or directory tree)."""
    await asyncio.to_thread(_sync_delete, path)


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


def _sync_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sync_delete(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
