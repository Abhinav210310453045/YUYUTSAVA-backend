"""``yuyutsava daemon --status`` and ``--stop``: asking about the daemon from outside it.

Answered here, from the discovery file and the process table, without importing
the daemon itself. Terminal output, so it lives with the rest of the CLI.
"""

from __future__ import annotations

import sys
import time as _time

from yuyutsava import platform as yplatform
from yuyutsava.daemon.options import build_parser
from yuyutsava.daemon.singleton import (
    daemon_discovery_path,
    daemon_lock_path,
    read_daemon_discovery,
)


def run_control(argv: list[str]) -> int | None:
    """Handle ``--status`` / ``--stop``; ``None`` when *argv* is neither."""
    args, _ = build_parser().parse_known_args(argv)
    if args.status:
        return daemon_status()
    if args.stop:
        return daemon_stop()
    return None


def daemon_status() -> int:
    """Print info about the running daemon. Exits 0 if running, 1 otherwise."""
    disco = read_daemon_discovery()
    if disco is None:
        print("yuyutsava daemon: not running", file=sys.stderr)
        return 1
    pid = disco.get("pid")
    web_url = disco.get("web_url") or "(unknown)"
    async_host_url = disco.get("async_host_url") or "(disabled)"
    started_at = disco.get("started_at")
    uptime_str = "(unknown)"
    if isinstance(started_at, (int, float)):
        secs = max(0, int(_time.time() - started_at))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h{m:02d}m{s:02d}s"
    print("yuyutsava daemon: running")
    print(f"  pid        = {pid}")
    print(f"  web_url    = {web_url}")
    print(f"  async_host = {async_host_url}")
    print(f"  uptime     = {uptime_str}")
    return 0


def daemon_stop() -> int:
    """Stop the running daemon (graceful terminate, escalate to kill) — cross-platform."""

    def _cleanup_files() -> None:
        for p in (daemon_lock_path(), daemon_discovery_path()):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    disco = read_daemon_discovery()
    if disco is None:
        print("yuyutsava daemon: not running (nothing to stop)", file=sys.stderr)
        return 0
    pid = disco.get("pid")
    if not isinstance(pid, int):
        print("yuyutsava daemon: discovery file is malformed", file=sys.stderr)
        return 1

    if not yplatform.pid_alive(pid):
        print(f"yuyutsava daemon: pid {pid} not alive; cleaning up stale files", file=sys.stderr)
        _cleanup_files()
        return 0

    print(f"yuyutsava daemon: stopping pid {pid}…", file=sys.stderr)
    # terminate_pid sends SIGTERM (TerminateProcess on Windows), waits up to
    # 5s, then escalates to a hard kill. The daemon's atexit hook normally
    # unlinks the lock/discovery files; we sweep them best-effort regardless.
    ok = yplatform.terminate_pid(pid, timeout=5.0)
    _cleanup_files()
    print(
        "yuyutsava daemon: stopped"
        if ok
        else "yuyutsava daemon: could not confirm stop (check for a stuck process)",
        file=sys.stderr,
    )
    return 0 if ok else 1
