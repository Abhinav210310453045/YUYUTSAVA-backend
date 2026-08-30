"""Refuse ``start_async_task`` when too many background tasks are already running.

Phase 4 step 4.4, second migration (was ``BackgroundTaskCapMiddleware``).

The cap is a soft one, enforced *before* launch. The alternative — letting the
launch succeed and killing it afterwards — costs a whole subagent startup to
learn something the mirror already knows.

Gates on :attr:`~yuyutsava.policy.types.ToolCall.resolved_tool`, not on the name
the model asked for. Those differ exactly when the model names a tool that is
not bound, and in that case this must stay out of the way so the framework's
unknown-tool path runs — the middleware this replaces carried a comment saying
so, and a sibling that omitted the same guard crashed turns (finding BA).
"""

from __future__ import annotations

import logging
from typing import Any

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Denied, ToolCall, ToolDecision

logger = logging.getLogger("yuyutsava.async_subagents.cap_policy")

_START_TOOL = "start_async_task"


class BackgroundTaskCapPolicy(Policy):
    """Cap concurrent background subagent launches.

    Parameters
    ----------
    mirror:
        The same ``AsyncTaskMirror`` the watcher updates. ``count_running()`` is
        the authority on how many are live.
    max_concurrent:
        Soft cap. ``8`` is a reasonable v1 upper bound; tune via daemon config
        once one exists.
    """

    name = "BackgroundTaskCapPolicy"

    def __init__(self, mirror: Any, max_concurrent: int = 8) -> None:
        super().__init__()
        self._mirror = mirror
        self._max = max_concurrent

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        if call.resolved_tool != _START_TOOL:
            return None
        running = self._mirror.count_running()
        if running < self._max:
            return None
        logger.info("rejecting start_async_task: cap %d reached", self._max)
        return Denied(
            f"Concurrency cap reached ({running}/{self._max} background tasks "
            "already running). Wait for a running task to complete (or call "
            "cancel_async_task) before starting another.",
            # Carried over from the middleware: this refusal reported itself as
            # an error and did not label the message with the tool name, where
            # the permission refusal did the opposite. See `Denied`.
            status="error",
            named=False,
        )


__all__ = ["BackgroundTaskCapPolicy"]
