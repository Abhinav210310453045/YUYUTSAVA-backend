"""Concurrency cap middleware for background subagent launches.

Wraps the supervisor's ``start_async_task`` tool. When the daemon-scoped
``AsyncTaskMirror`` already tracks ``max_concurrent`` non-terminal tasks, the
call is short-circuited to a ``ToolMessage`` that tells the master to retry
later. This avoids the more expensive reactive "kill after start" path.

Composes with ``AsyncSubAgentMiddleware`` — list this middleware AFTER it so
its ``wrap_tool_call`` sees the start tool as the handler.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger("yuyutsava.async_subagents.cap_middleware")


class BackgroundTaskCapMiddleware(AgentMiddleware):
    """Refuses ``start_async_task`` calls when the mirror is at capacity.

    Parameters
    ----------
    mirror:
        Same ``AsyncTaskMirror`` the watcher updates. Reads ``count_running()``
        to decide.
    max_concurrent:
        Soft cap. The default of ``8`` is a reasonable upper bound for v1; tune
        via daemon config when we add one.
    """

    def __init__(self, mirror, max_concurrent: int = 8) -> None:
        super().__init__()
        self._mirror = mirror
        self._max = max_concurrent

    def _refusal(self, request: Any) -> ToolMessage:
        tool_call_id = request.tool_call.get("id", "")
        msg = (
            f"Concurrency cap reached ({self._mirror.count_running()}/{self._max} "
            "background tasks already running). Wait for a running task to "
            "complete (or call cancel_async_task) before starting another."
        )
        return ToolMessage(content=msg, tool_call_id=tool_call_id, status="error")

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        # request.tool is None when the model's tool call didn't resolve to a
        # bound tool (hallucinated/mistyped name) — let handler() run the
        # normal unknown-tool path instead of crashing on `.name`.
        if (
            request.tool is not None
            and request.tool.name == "start_async_task"
            and self._mirror.count_running() >= self._max
        ):
            logger.info("rejecting start_async_task: cap %d reached", self._max)
            return self._refusal(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        if (
            request.tool is not None
            and request.tool.name == "start_async_task"
            and self._mirror.count_running() >= self._max
        ):
            logger.info("rejecting start_async_task: cap %d reached", self._max)
            return self._refusal(request)
        return await handler(request)
