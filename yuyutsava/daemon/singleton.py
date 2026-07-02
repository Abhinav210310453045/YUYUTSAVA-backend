"""Daemon singleton enforcement + discovery (one daemon per user profile).

Two files live in ``state_dir()`` while a daemon is running:

* ``daemon.lock`` — held exclusive via a cross-platform ``FileLock`` for the
  daemon's lifetime. A second ``yuyutsava daemon`` invocation tries to acquire it,
  fails non-blockingly, and exits cleanly.
* ``daemon.json`` — discovery payload (``{pid, web_url, async_host_url,
  started_at}``). Read by ``yuyutsava daemon --status``, ``--stop``, and
  any client that wants to find the running daemon (CLI ``attach``, the
  Electron app, etc.).

Stale-lock recovery: if the lockfile is held but the recorded PID is
gone (process died without releasing), the helpers detect that on the
next ``acquire`` and unlink the leftover files once before retrying.

Cross-platform: the lock is a :class:`yuyutsava.platform.FileLock`
(portalocker under the hood — msvcrt on Windows, flock on POSIX).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from yuyutsava.platform import FileLock, pid_alive
from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.daemon.singleton")


def daemon_lock_path() -> Path:
    return state_dir() / "daemon.lock"


def daemon_discovery_path() -> Path:
    return state_dir() / "daemon.json"


def _is_pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def read_daemon_discovery() -> dict[str, Any] | None:
    """Return the parsed discovery payload, or ``None`` if missing / stale."""
    path = daemon_discovery_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or not _is_pid_alive(pid):
        return None
    return data


def write_daemon_discovery(
    *,
    pid: int,
    web_url: str,
    async_host_url: str | None,
) -> None:
    """Atomically write the discovery file."""
    payload = {
        "pid": pid,
        "web_url": web_url,
        "async_host_url": async_host_url,
        "started_at": time.time(),
    }
    path = daemon_discovery_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _unlink_safe(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("could not unlink %s", path, exc_info=True)


# Module-level handle so a fd-less ``release_daemon_lock()`` (e.g. from an
# atexit hook that lost the reference) can still find and drop the lock.
_HELD_LOCK: FileLock | None = None


def acquire_daemon_lock() -> FileLock | None:
    """Try to take exclusive ownership of the daemon lockfile.

    Returns the held :class:`FileLock` (caller MUST keep it alive for the
    daemon's lifetime) or ``None`` if another live daemon already owns the
    lock. The caller is expected to call :func:`release_daemon_lock` on exit;
    cleanup also runs via ``atexit`` registration in
    :func:`register_daemon_cleanup`.
    """
    global _HELD_LOCK
    path = daemon_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in (1, 2):
        lock = FileLock(path)
        if lock.acquire(blocking=False):
            # Acquired: record our pid in the lockfile body for postmortem.
            lock.stamp(f"{os.getpid()}\n")
            _HELD_LOCK = lock
            return lock
        # Lock held — but by a live PID?
        if read_daemon_discovery() is not None:
            # Live daemon — refuse.
            return None
        # Stale lockfile from a dead PID. Unlink both and retry once.
        if attempt == 1:
            _unlink_safe(daemon_discovery_path())
            _unlink_safe(path)
            logger.warning("cleared stale daemon lock; retrying")
            continue
        return None
    return None


def release_daemon_lock(lock: FileLock | None) -> None:
    """Release the lock and remove the lock + discovery files.

    Safe to call multiple times — both unlink steps tolerate missing files.
    """
    global _HELD_LOCK
    if lock is None:
        lock = _HELD_LOCK
    if lock is not None:
        lock.release()
    _HELD_LOCK = None
    _unlink_safe(daemon_discovery_path())
    _unlink_safe(daemon_lock_path())


def register_daemon_cleanup(lock: FileLock) -> None:
    """Register an ``atexit`` cleanup so SIGTERM / crashes also unlink files."""
    import atexit

    atexit.register(release_daemon_lock, lock)
