"""Letting a background task finish during teardown.

Every subsystem that owns a task stops it the same way — cancel, then await
it so it is not left pending — and every copy used to swallow whatever the
await raised, including a real failure the task died of. :func:`reap` keeps
the part teardown needs (it always returns) and drops the part it does not
(silence): the task's own cancellation is expected and ignored, anything else
it ended with is logged, because by teardown there is nobody else to tell.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("yuyutsava.aio.tasks")


async def reap(task: asyncio.Task | None, *, what: str) -> None:
    """Wait for *task* to end, however it ends. Never raises.

    ``what`` names the task in the log line if it ended with an error.
    """
    if task is None:
        return
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown must finish; the failure is logged
        logger.debug("%s ended with an error during teardown", what, exc_info=True)


async def cancel_and_reap(task: asyncio.Task | None, *, what: str) -> None:
    """Cancel *task* (a no-op once it is done), then :func:`reap` it."""
    if task is None:
        return
    task.cancel()
    await reap(task, what=what)
