"""How a background subagent's question reaches the person at the terminal.

There are two ways, and picking the wrong one hangs a prompt.

**Defer to the daemon when one is running.** It owns the async host and the
single idempotent decision pipeline, so the chat consumes its SSE and answers
over REST. An approval can then be answered from the CLI *or* the desktop app
and stay in sync.

**Run a watcher locally only when this process owns the host.** Two watchers on
one host both discover the same interrupt and both route it — the de-duplication
inside a watcher is per-process and cannot see the other one. So the user is
asked twice, and whichever answers first resumes the run while the other is
still waiting on a future nobody will resolve.

That is not hypothetical: attaching to a running daemon's host still hands this
process a URL and a mirror, so "there is a host" is not the same question as
"is it mine".
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
from yuyutsava.cli.async_hitl import CliHitlBridge, CliRemoteHitl
from yuyutsava.cli.remote_attach import CliAttachClient
from yuyutsava.daemon.singleton import read_daemon_discovery


def loopback_url(url: str) -> str:
    """Rewrite a daemon web URL to loopback so the CLI is auth-exempt.

    The daemon may advertise a non-loopback bind (e.g. ``http://0.0.0.0:7654``)
    with bearer auth enforced; connecting from 127.0.0.1 is exempt.
    """
    return url.replace("://0.0.0.0", "://127.0.0.1").replace("://[::]", "://127.0.0.1")


@dataclass
class BackgroundApprovals:
    """How a background subagent's approval request reaches this chat.

    Preferred: when a daemon is running it owns the async host and the single
    idempotent decision pipeline, so the chat defers to it — consume the
    daemon's SSE, answer over REST. An approval can then be answered from the
    CLI *or* the desktop app and stay in sync, and the prompt never freezes,
    because there is no competing stdin reader and no double-resume.

    Fallback: only when this chat OWNS the host — no daemon running — does it
    run the in-process watcher that prompts locally.
    """

    bridge: Any | None = None
    watcher: Any | None = None
    remote: Any | None = None

    @classmethod
    async def open(cls, bundle: Any) -> "BackgroundApprovals":
        if bundle.async_host_url is None:
            # Nothing runs in the background, so nothing can ask.
            return cls()
        disco = read_daemon_discovery()
        daemon_web = disco.get("web_url") if isinstance(disco, dict) else None
        if daemon_web:
            remote = CliRemoteHitl(
                CliAttachClient(base_url=loopback_url(str(daemon_web)), label="yuyutsava-chat")
            )
            await remote.start()
            return cls(remote=remote)
        if bundle.async_task_mirror is not None:
            bridge = CliHitlBridge()
            watcher = AsyncTaskHealthWatcher(
                mirror=bundle.async_task_mirror,
                host_url=bundle.async_host_url,
                ask_handler=bridge.post_ask,
                event_sink=bridge.post_event,
                agent_path_root="cli",
            )
            await watcher.start()
            return cls(bridge=bridge, watcher=watcher)
        return cls()

    async def render_between_turns(self) -> None:
        """Show what the background did while the user was composing.

        Between turns, not during one: interleaving it with a streaming reply is
        how the transcript became unreadable.
        """
        if self.bridge is None:
            return
        with contextlib.suppress(Exception):
            await self.bridge.render_between_turns()

    async def aclose(self) -> None:
        if self.watcher is not None:
            with contextlib.suppress(Exception):
                await self.watcher.shutdown()
        if self.remote is not None:
            with contextlib.suppress(Exception):
                await self.remote.stop()


__all__ = ["BackgroundApprovals", "loopback_url"]
