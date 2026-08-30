"""Stop ``check_async_task`` poll-loops and compact the errors it returns.

Phase 4 step 4.4, fourth migration (was ``CheckAsyncTaskGuardMiddleware``), and
**the policy that justifies the** :class:`~yuyutsava.policy.types.Raw` **escape
hatch** ADR-004 predicted would be needed.

deepagents' ``check_async_task`` always re-hits the server and returns the raw
status — including a full ``run.error`` traceback on failure. When a background
task fails, the master can mis-read the ambiguous error as transient and poll
the tool dozens of times until it blows the graph recursion limit and the whole
turn crashes. It also injects the raw traceback into context.

Two jobs, without touching the vendored deepagents tool:

1. **Terminal short-circuit** (``before_tool``). Once a ``task_id`` has been seen
   in a terminal state (``success``/``error``/``cancelled``/``timeout``), any
   further check for that id returns the cached result with a "do not re-check"
   note instead of re-querying — capping the loop regardless of what the model
   decides.
2. **Error compaction** (``after_tool``). When a check returns ``status=error``,
   the possibly-huge ``error`` field is reduced to a one-line reason via
   ``watcher.compact_error`` so it never bloats the launching agent's context.
   The full text stays available through ``GET /tasks/{id}/logs``.

## Why ``Raw``

The short-circuit returns a LangGraph ``Command``, not a refusal. There is no
YUYUTSAVA-level meaning to express — the policy is replaying a framework object
the framework will merge into state. Modelling that as a ``Denied`` would be a
lie (nothing was denied; a cached success is being returned), and giving the
protocol a "return a state update" concept for one caller would widen it for
everyone. ADR-004: *"grant it explicitly and document it rather than weakening
the protocol."* This is that grant, and it is the only one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Raw, ToolCall, ToolDecision

logger = logging.getLogger("yuyutsava.async_subagents.check_guard_policy")

_CHECK_TOOL = "check_async_task"
_TERMINAL = frozenset({"success", "error", "cancelled", "timeout"})


class CheckAsyncTaskGuardPolicy(Policy):
    """Cap repeated ``check_async_task`` polling; compact its error payloads."""

    name = "CheckAsyncTaskGuardPolicy"

    def __init__(self) -> None:
        super().__init__()
        # task_id -> compacted terminal result dict (status is terminal).
        self._terminal: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        task_id = self._task_id(call)
        if task_id and task_id in self._terminal:
            return Raw(self._cached_command(call, task_id))
        return None

    async def after_tool(self, call: ToolCall, result: Any) -> Any:
        """Compact errors and remember terminal statuses."""
        from langgraph.types import Command

        task_id = self._task_id(call)
        if task_id is None or not isinstance(result, Command):
            return result
        update = result.update if isinstance(result.update, dict) else None
        msgs = (update or {}).get("messages")
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _task_id(self, call: ToolCall) -> str | None:
        """The task this call is about, or ``None`` if it is not a check call.

        Keyed on the **resolved** tool, matching the middleware's
        ``getattr(request.tool, "name", None) == _CHECK_TOOL``. A model naming a
        tool that is not bound must fall through to the framework's unknown-tool
        path, not be answered from this cache.
        """
        if call.resolved_tool != _CHECK_TOOL:
            return None
        tid = (call.args.get("task_id") or "").strip()
        return tid or None

    def _cached_command(self, call: ToolCall, task_id: str) -> Any:
        from langchain_core.messages import ToolMessage
        from langgraph.types import Command

        payload = dict(self._terminal[task_id])
        payload["note"] = (
            "This task already finished — returning the cached terminal result. "
            "Do not call check_async_task again for this task_id; report the "
            "outcome to the user or relaunch with start_async_task."
        )
        return Command(update={
            "messages": [ToolMessage(json.dumps(payload), tool_call_id=call.id)],
        })


__all__ = ["CheckAsyncTaskGuardPolicy"]
