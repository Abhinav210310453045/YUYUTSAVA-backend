"""
Process lifecycle: signal handling, graceful shutdown, ordered teardown.

The daemon installs SIGINT/SIGTERM handlers that set a single
``asyncio.Event``. All the loops watch that event and exit cleanly; the
``yuyutsava daemon`` entry point awaits ordered shutdown of:

    sources -> bus drain -> orchestrator/triage loops -> channels -> store

Order matters: we stop sources first so no new events arrive, drain the bus,
then let the in-flight orchestrator task complete, then close channels, then
flush the store's writer.
"""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger("yuyutsava.daemon.lifecycle")


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle(signame: str) -> None:
        logger.info("received %s, shutting down", signame)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig.name)
        except NotImplementedError:
            # Windows / restricted env: Ctrl-C still raises KeyboardInterrupt.
            pass


def install_reload_handler(reload_event: asyncio.Event) -> None:
    """Set *reload_event* on SIGHUP so main.py can re-read configs.

    Main owns the event; consumers (e.g. MCP loader) re-read their config file
    and diff against the running state. Best-effort: SIGHUP isn't supported on
    Windows, so we silently skip there.
    """
    loop = asyncio.get_running_loop()

    def _handle() -> None:
        logger.info("received SIGHUP, scheduling config reload")
        reload_event.set()

    try:
        loop.add_signal_handler(signal.SIGHUP, _handle)
    except (AttributeError, NotImplementedError):
        # SIGHUP missing on Windows; not a fatal error.
        pass
