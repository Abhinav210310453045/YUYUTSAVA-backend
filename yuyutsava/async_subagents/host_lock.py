"""Shared ``AsyncSubagentHost`` ownership: first-come, lock-protected.

Both the daemon's bootstrap and the CLI's agent stack used to construct
their own ``AsyncSubagentHost`` (a thread running ``langgraph_api.cli.run_server``
on an ephemeral port). When both ran at once, two LangGraph dev servers
existed on different ports — pure waste.

This module makes the host a profile-wide singleton via a lockfile +
discovery file under ``state_dir()``:

* ``async_host.lock`` — held exclusive (cross-platform ``FileLock``) by whichever
  process owns the host.
* ``async_host.json`` — ``{pid, url, started_at, graph_ids}``. Other
  processes read this to attach.

API
---
The single entry point is :func:`acquire_or_attach_host`. The caller
hands in a zero-arg ``factory`` that returns a freshly-built
``AsyncSubagentHost`` (with graphs already registered) and gets back the
URL plus an optional ownership handle (host + fd). When the handle is
``(None, None)``, the caller is an attacher and must not call
``host.shutdown()`` on exit.

Stale-lock recovery
-------------------
If the lockfile exists but the recorded PID is dead, the helpers unlink
both files once and retry the acquisition.

Cross-platform: the lock is a :class:`yuyutsava.platform.FileLock`
(portalocker under the hood).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from yuyutsava.platform import FileLock, pid_alive
from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.async_subagents.host_lock")


def host_lock_path() -> Path:
    return state_dir() / "async_host.lock"


def host_discovery_path() -> Path:
    return state_dir() / "async_host.json"


def _is_pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _unlink_safe(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("could not unlink %s", path, exc_info=True)


def _ping_ok(url: str, *, timeout: float = 1.5) -> bool:
    """Return True iff ``{url}/ok`` answers 200."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/ok", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


def read_host_discovery() -> dict[str, Any] | None:
    """Return the parsed discovery payload if a live host owns it."""
    path = host_discovery_path()
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


def _write_host_discovery(*, pid: int, url: str, graph_ids: list[str]) -> None:
    payload = {
        "pid": pid,
        "url": url,
        "graph_ids": graph_ids,
        "started_at": time.time(),
    }
    path = host_discovery_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class HostAttachment:
    """Result of :func:`acquire_or_attach_host`.

    Owner mode: ``host`` and ``fd`` are set; caller must call
    :func:`release_host_lock` on shutdown.

    Attacher mode: ``host`` and ``lock`` are both ``None``; ``url`` points
    at a running host owned by another process. Caller must not call
    ``host.shutdown()`` (it doesn't own one).
    """
    url: str
    host: Any | None      # AsyncSubagentHost — duck-typed to avoid cycle
    lock: FileLock | None


def acquire_or_attach_host(*, factory: Callable[[], Any]) -> HostAttachment:
    """Either acquire ownership of the profile-wide host, or attach to it.

    ``factory`` is a zero-arg callable that returns a fresh
    ``AsyncSubagentHost`` instance with its graphs already populated; it
    is only invoked if this process becomes the owner. The factory must
    not have started the host yet — we call ``.start()`` here.

    Returns a :class:`HostAttachment`. Owner mode carries an fd that the
    caller must keep open for the host's lifetime.
    """
    lock_path = host_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in (1, 2):
        lock = FileLock(lock_path)
        if not lock.acquire(blocking=False):
            # Lock held — check discovery + health.
            disco = read_host_discovery()
            if disco is not None and _ping_ok(str(disco.get("url", ""))):
                logger.info("attaching to existing async host at %s (pid=%s)",
                            disco.get("url"), disco.get("pid"))
                return HostAttachment(url=str(disco["url"]), host=None, lock=None)
            # Stale lock or dead host — clean up once and retry.
            if attempt == 1:
                _unlink_safe(host_discovery_path())
                _unlink_safe(lock_path)
                logger.warning("cleared stale async-host lock; retrying")
                continue
            raise RuntimeError(
                "async host lock held but discovery is stale/unhealthy and "
                "stale-lock recovery already retried once"
            )

        # We own the lock — build, start, publish.
        try:
            host = factory()
            host.start()
            url = host.url
            _write_host_discovery(
                pid=os.getpid(),
                url=url,
                graph_ids=list(getattr(host, "graph_ids", []) or []),
            )
            # Record our pid in the lock body for postmortem.
            lock.stamp(f"{os.getpid()}\n")
            logger.info("acquired async host ownership on %s", url)
            return HostAttachment(url=url, host=host, lock=lock)
        except Exception:
            # Roll back: release the lock, unlink, surface the error.
            lock.release()
            _unlink_safe(host_discovery_path())
            _unlink_safe(lock_path)
            raise

    # Unreachable — both branches above either return or raise.
    raise RuntimeError("acquire_or_attach_host: exhausted retries")


def release_host_lock(attachment: HostAttachment) -> None:
    """Tear down the host (if owner) and clean up lock + discovery files.

    Safe to call multiple times. No-op for an attacher (no fd, no host).
    """
    if attachment.host is not None:
        try:
            attachment.host.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("async host shutdown failed (continuing)")
    if attachment.lock is not None:
        attachment.lock.release()
        _unlink_safe(host_discovery_path())
        _unlink_safe(host_lock_path())


def register_host_cleanup(attachment: HostAttachment) -> None:
    """Register an ``atexit`` cleanup for the host attachment."""
    import atexit

    atexit.register(release_host_lock, attachment)
