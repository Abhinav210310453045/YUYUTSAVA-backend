"""OS-invariant process management.

Replaces POSIX-only idioms (``os.kill(pid, 0)``, ``os.kill(pid, SIGTERM)``,
``os.getpgid``/``os.killpg``, ``start_new_session=True``) with psutil-backed
helpers that behave the same on Windows, macOS and Linux.

Windows notes baked in here so callers never think about them:
* No SIGTERM/SIGKILL — psutil ``terminate()`` maps to TerminateProcess.
* No process groups — ``kill_tree`` walks psutil children recursively instead
  of signalling a pgid.
* ``start_new_session`` is silently ignored — detached spawn uses
  ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` creationflags.
* ``signal.signal`` only accepts a small signal set — handler installation
  registers SIGBREAK instead of SIGHUP-style extras.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger("yuyutsava.platform.process")

_IS_WINDOWS = sys.platform == "win32"


def pid_alive(pid: int) -> bool:
    """True if *pid* refers to a live process (cross-platform ``kill(pid, 0)``)."""
    if pid <= 0:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:  # psutil should not throw here, but never let this crash callers
        return False


def terminate_pid(pid: int, *, timeout: float = 5.0, then_kill: bool = True) -> bool:
    """Gracefully terminate *pid*; escalate to a hard kill after *timeout*.

    Returns True when the process is gone (or was already gone), False when it
    survived even the hard kill. Never raises for a missing process.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        if not then_kill:
            return False
        try:
            proc.kill()
            proc.wait(timeout=2.0)
            return True
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            return not pid_alive(pid)
    except psutil.AccessDenied:
        return False


def kill_tree(pid: int, *, timeout: float = 5.0) -> None:
    """Terminate *pid* and every descendant (replaces ``os.killpg``).

    Children are collected before the parent dies (so nothing is orphaned),
    terminated gracefully, then force-killed after *timeout*.
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        procs = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        procs = []
    procs.append(parent)

    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def spawn_detached(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
) -> subprocess.Popen:
    """Spawn *cmd* detached from our terminal/session on any OS.

    POSIX: ``start_new_session=True`` (own session + process group, so a
    Ctrl+C on us never reaches the child, and ``kill_tree`` can reap it).
    Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` — the closest
    equivalent (no console, own group for CTRL_BREAK delivery). The executable
    is also resolved via ``shutil.which`` there: CreateProcess only finds
    ``.exe`` files by bare name, while ``which`` honours PATHEXT — without it,
    batch-shim commands like ``npm`` (really ``npm.cmd``) never launch.
    """
    cmd = list(cmd)
    if _IS_WINDOWS:
        resolved = shutil.which(cmd[0])
        if resolved is not None:
            cmd[0] = resolved
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def install_terminate_handler(callback: Callable[[], None]) -> None:
    """Register *callback* for the "please stop" signals available on this OS.

    POSIX: SIGINT + SIGTERM. Windows: SIGINT + SIGBREAK (SIGTERM cannot be
    delivered to a Windows process; TerminateProcess is not catchable).
    Intended for standalone child processes (_voice_proc / _webcam_proc).
    """

    def _handler(_signum, _frame) -> None:
        callback()

    signal.signal(signal.SIGINT, _handler)
    if _IS_WINDOWS:
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal.signal(sigbreak, _handler)
    else:
        signal.signal(signal.SIGTERM, _handler)
