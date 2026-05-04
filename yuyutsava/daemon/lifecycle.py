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
