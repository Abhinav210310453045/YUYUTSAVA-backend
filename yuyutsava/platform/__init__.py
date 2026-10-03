"""Platform substrate — the one place OS-specific primitives live.

Everything else in the codebase stays OS-invariant by routing through this package:

* :mod:`~yuyutsava.platform.filelock` — cross-process exclusive file locks
  (portalocker, with an fcntl fallback on POSIX).
* :mod:`~yuyutsava.platform.process` — pid liveness, terminate and kill, detached
  spawn and kill-tree (psutil; no POSIX signals or process groups leak out).
* :mod:`~yuyutsava.platform.hostprofile` — cached facts about the host (family,
  shell, package managers, critical paths) that drive prompts, zone classification
  and shell selection.
* :mod:`~yuyutsava.platform.elevation` — per-operation privilege elevation (UAC,
  admin osascript, pkexec) behind one interface.
* :mod:`~yuyutsava.platform.files` — replacing a file in one step, optionally
  readable by its owner alone (mode 0600, or an owner-only ACL on Windows).

The names below are re-exported because other modules use them from here — some
through ``from yuyutsava import platform``; import anything else, such as
``run_capture`` or ``HostProfile``, from the module that owns it.
"""

from __future__ import annotations

from yuyutsava.platform.filelock import FileLock
from yuyutsava.platform.hostprofile import host_profile
from yuyutsava.platform.process import (
    install_terminate_handler,
    kill_tree,
    pid_alive,
    spawn_detached,
    terminate_pid,
)

__all__ = [
    "FileLock",
    "host_profile",
    "install_terminate_handler",
    "kill_tree",
    "pid_alive",
    "spawn_detached",
    "terminate_pid",
]
