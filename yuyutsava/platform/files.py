"""Replacing a file's contents in one step, optionally readable by its owner alone.

A reader of a file written here sees the old contents or the new, never half of
each: the text goes to a temporary file beside the target, is flushed to disk,
and then renamed over it, which is atomic on one volume on every OS.

:func:`replace_private_text` is for files that hold secrets. The temporary file
is owner-only *before* the secret is written into it, so there is no moment when
the secret sits in a file other users can read:

* POSIX — the temporary file is created with mode 0600.
* Windows — mode bits mean nothing to the NTFS ACL, so inherited entries are
  removed with ``icacls`` and full control is granted to the current user's SID
  alone, before anything is written.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

#: Mode of an ordinary file written here, before the umask.
_SHARED_MODE = 0o644
#: Mode of a private file: read and write for the owner, nothing for anyone else.
_PRIVATE_MODE = 0o600

#: How long a rename keeps retrying while another process holds the target open.
#: Windows refuses to replace an open file; POSIX never needs this.
_REPLACE_PATIENCE_SECONDS = 2.0


def replace_text(path: Path, text: str) -> None:
    """Replace *path* with *text* (UTF-8) in one step."""
    _replace(path, text, mode=_SHARED_MODE, restrict=None)


def replace_private_text(path: Path, text: str) -> None:
    """Replace *path* with *text* (UTF-8) in one step, readable by its owner alone."""
    _replace(path, text, mode=_PRIVATE_MODE, restrict=_restrict_to_current_user)


def _replace(path: Path, text: str, *, mode: int, restrict: Callable[[Path], None] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        if restrict is not None:
            restrict(temp)
        with os.fdopen(fd, "wb") as handle:
            fd = -1  # owned by the handle now
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _rename_over(temp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def _rename_over(source: Path, target: Path) -> None:
    deadline = time.monotonic() + _REPLACE_PATIENCE_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not _IS_WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _restrict_to_current_user(path: Path) -> None:
    """Make *path* owner-only on Windows; POSIX already did it with the mode."""
    if not _IS_WINDOWS:
        return
    sid = _current_user_sid()
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            f"could not make {path} readable by its owner alone "
            f"(icacls exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()})"
        )


def _current_user_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=False
    )
    # One CSV row: "DOMAIN\user","S-1-5-21-…"
    sid = result.stdout.strip().rsplit(",", 1)[-1].strip('"')
    if result.returncode != 0 or not sid.startswith("S-"):
        raise OSError(f"could not read the current user's SID (whoami exited {result.returncode})")
    return sid


__all__ = ["replace_private_text", "replace_text"]
