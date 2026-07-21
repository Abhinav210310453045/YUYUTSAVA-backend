"""Platform substrate — the ONE place OS-specific primitives live.

Everything else in the codebase stays OS-invariant by routing through this
package:

* :mod:`filelock`    — cross-process exclusive file locks (portalocker;
                       fcntl fallback on POSIX so an unsynced env keeps working).
* :mod:`process`     — pid liveness, terminate/kill, detached spawn, kill-tree
                       (psutil; no POSIX signals or process groups leak out).
* :mod:`hostprofile` — the "OS passport": cached facts about the host
                       (family, shell, package managers, critical paths) that
                       drive prompts, zone classification, and shell selection.
* :mod:`elevation`   — per-operation privilege elevation (UAC / admin
                       osascript / pkexec) behind one reusable interface.

Import from the package root::

    from yuyutsava.platform import FileLock, host_profile, pid_alive
"""

from __future__ import annotations

from yuyutsava.platform.filelock import FileLock
from yuyutsava.platform.hostprofile import HostProfile, host_profile
from yuyutsava.platform.process import (
    install_terminate_handler,
    kill_tree,
    pid_alive,
    run_capture,
    spawn_detached,
    terminate_pid,
)

__all__ = [
    "FileLock",
    "HostProfile",
    "host_profile",
    "install_terminate_handler",
    "kill_tree",
    "pid_alive",
    "run_capture",
    "spawn_detached",
    "terminate_pid",
]
