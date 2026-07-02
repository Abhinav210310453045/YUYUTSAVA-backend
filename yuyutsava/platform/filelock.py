"""Cross-platform exclusive file lock.

Replaces the raw ``os.open`` + ``fcntl.flock`` blocks that used to live in
``storage/base.py`` (migration lock), ``daemon/singleton.py`` (single-daemon
lock) and ``async_subagents/host_lock.py`` (host election).

Backend: `portalocker` (msvcrt region locks on Windows, flock on POSIX).
If portalocker is not installed — e.g. a dev env that has not re-synced —
we fall back to ``fcntl`` directly on POSIX so nothing regresses; on Windows
portalocker is mandatory and a clear error is raised.

Semantics are uniform on every OS: ``acquire`` always takes the lock
non-blockingly under the hood and polls, so "blocking" behaviour does not
depend on platform quirks (msvcrt's LK_LOCK only retries for ~10s).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from types import TracebackType

logger = logging.getLogger("yuyutsava.platform.filelock")

try:  # preferred cross-platform backend
    import portalocker as _portalocker
except ImportError:  # pragma: no cover - exercised only on unsynced envs
    _portalocker = None
    if sys.platform != "win32":
        import fcntl as _fcntl
    else:  # Windows has no fcntl and no fallback
        raise ImportError(
            "portalocker is required on Windows for cross-process file locks "
            "(run `uv sync` to install it)"
        )


def _try_lock(fh) -> bool:
    """One non-blocking exclusive-lock attempt. Returns False if held elsewhere."""
    if _portalocker is not None:
        try:
            _portalocker.lock(fh, _portalocker.LOCK_EX | _portalocker.LOCK_NB)
            return True
        except _portalocker.exceptions.BaseLockException:
            return False
    try:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(fh) -> None:
    if _portalocker is not None:
        _portalocker.unlock(fh)
    else:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


class FileLock:
    """Exclusive cross-process lock on *path*.

    Usage patterns served:

    * short critical section (migrations)::

          with FileLock(path):
              ...

    * held-for-process-lifetime with non-blocking probe (daemon singleton)::

          lock = FileLock(path)
          if not lock.acquire(blocking=False):
              ...  # someone else owns it
          lock.stamp(f"{os.getpid()}\\n")
          ...
          lock.release()
    """

    _POLL_INTERVAL = 0.1

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def locked(self) -> bool:
        return self._fh is not None

    def acquire(self, *, blocking: bool = True, timeout: float | None = None) -> bool:
        """Take the lock. Returns False (instead of raising) when *blocking*
        is False — or when *timeout* seconds elapse — and the lock is held
        by another process.
        """
        if self._fh is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        fh = os.fdopen(fd, "r+b", buffering=0)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if _try_lock(fh):
                    self._fh = fh
                    return True
                if not blocking:
                    fh.close()
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(self._POLL_INTERVAL)
        except BaseException:
            fh.close()
            raise

    def stamp(self, text: str) -> None:
        """Truncate the lockfile and write *text* (e.g. the owner pid)."""
        if self._fh is None:
            raise RuntimeError("stamp() requires the lock to be held")
        try:
            self._fh.seek(0)
            self._fh.truncate(0)
            self._fh.write(text.encode())
            self._fh.flush()
        except OSError:
            logger.debug("could not stamp lockfile %s", self._path, exc_info=True)

    def release(self) -> None:
        """Unlock and close. Safe to call multiple times; keeps the file on disk."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def __enter__(self) -> "FileLock":
        self.acquire(blocking=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
