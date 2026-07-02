"""Stop ``check_async_task`` poll-loops and compact the errors it returns.

deepagents' ``check_async_task`` always re-hits the server and returns the raw
status — including a full ``run.error`` traceback on failure. When a background
task fails, the master can mis-read the ambiguous error as transient and poll
the tool dozens of times until it blows the graph recursion limit and the whole
turn crashes (see the original repro). It also injects the raw traceback into
context.

This middleware sits next to ``AsyncTaskInterruptPatchMiddleware`` on the master
and does two things, without touching the vendored deepagents tool:

1. **Terminal short-circuit.** Once a ``task_id`` has been observed in a terminal
   state (``success``/``error``/``cancelled``/``timeout``), any further
   ``check_async_task`` for that id returns the cached terminal result with a
   "do not re-check" note instead of re-querying — capping the loop regardless of
   what the model decides.
2. **Error compaction.** When a check returns ``status=error``, the (possibly
   huge) ``error`` field is reduced to a short one-line reason via
   ``watcher.compact_error`` so it never bloats the launching agent's context.
   The full text stays available through ``GET /tasks/{id}/logs``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger("yuyutsava.async_subagents.check_guard_middleware")

_CHECK_TOOL = "check_async_task"
_TERMINAL = frozenset({"success", "error", "cancelled", "timeout"})


class CheckAsyncTaskGuardMiddleware(AgentMiddleware):
    """Cap repeated check_async_task polling + compact its error payloads."""

    def __init__(self) -> None:
        super().__init__()
        # task_id -> compacted terminal result dict (status is terminal).
        self._terminal: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        task_id = self._task_id(request)
        if task_id and task_id in self._terminal:
            return self._cached_command(request, task_id)
        result = handler(request)
        return self._post(request, task_id, result)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        task_id = self._task_id(request)
        if task_id and task_id in self._terminal:
            return self._cached_command(request, task_id)
        result = await handler(request)
        return self._post(request, task_id, result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_check(request: Any) -> bool:
        tool = getattr(request, "tool", None)
        return getattr(tool, "name", None) == _CHECK_TOOL

    def _task_id(self, request: Any) -> str | None:
        if not self._is_check(request):
            return None
        args = (getattr(request, "tool_call", None) or {}).get("args") or {}
        tid = (args.get("task_id") or "").strip()
        return tid or None

    @staticmethod
    def _tool_call_id(request: Any) -> str | None:
        return (getattr(request, "tool_call", None) or {}).get("id")

    def _cached_command(self, request: Any, task_id: str) -> Command:
        payload = dict(self._terminal[task_id])
        payload["note"] = (
            "This task already finished — returning the cached terminal result. "
            "Do not call check_async_task again for this task_id; report the "
            "outcome to the user or relaunch with start_async_task."
        )
        return Command(update={
            "messages": [ToolMessage(json.dumps(payload), tool_call_id=self._tool_call_id(request))],
        })

    def _post(self, request: Any, task_id: str | None, result: Any) -> Any:
        """Inspect the handler result; compact errors and cache terminal status."""
        if task_id is None or not isinstance(result, Command):
            return result
        msgs = (result.update or {}).get("messages") if isinstance(result.update, dict) else None
        if not msgs:
            return result
        tm = msgs[0]
        content = getattr(tm, "content", None)
        if not isinstance(content, str):
            return result
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return result
        if not isinstance(data, dict):
            return result
        status = data.get("status")
        if status not in _TERMINAL:
            return result
        # Compact the error so it never bloats context.
        if status == "error" and data.get("error"):
            from yuyutsava.async_subagents.watcher import compact_error
            data["error"] = compact_error(data.get("error"))
        self._terminal[task_id] = data
        # Re-serialize the (possibly compacted) terminal result in place.
        try:
            tm.content = json.dumps(data)
        except Exception:  # noqa: BLE001  # leave original content if we can't
            logger.debug("could not re-serialize compacted check result", exc_info=True)
        return result
