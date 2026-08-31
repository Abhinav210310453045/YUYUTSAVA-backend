"""Heal orphaned tool calls on a background thread before interrupting it.

Phase 4 step 4.4, fifth migration (was ``AsyncTaskInterruptPatchMiddleware``).

When the master calls ``update_async_task`` or ``cancel_async_task``, the remote
subagent's thread may have an ``AIMessage`` carrying tool_calls that no
``ToolMessage`` ever answered. Interrupting on top of that leaves the thread in a
state most providers reject outright. This settles the in-flight run and injects
synthetic ToolMessages so the orphaned calls are resolved first.

The heavy lifting — LangGraph SDK clients, run settling, thread patching, and the
pure ``find_pending_tool_calls`` / ``build_synthetic_toolmessages`` helpers the
watcher also uses — stays in
:mod:`yuyutsava.async_subagents.interrupt_middleware`. None of it is policy; it
is an SDK client with a job. What moves here is the decision: *which* calls
trigger a patch, and against which thread.

## The bug this migration removes

The middleware read ``request.tool.name`` with no ``None`` check. ``request.tool``
is ``None`` whenever the model names a tool that is not bound — a hallucination
or a typo — so any such call raised::

    AttributeError: 'NoneType' object has no attribute 'name'

killing the whole turn instead of taking the framework's unknown-tool path. Its
sibling ``BackgroundTaskCapMiddleware`` guarded exactly this case and said why in
a comment; this one did not, and nothing noticed (finding BA).

Comparing against :attr:`~yuyutsava.policy.types.ToolCall.resolved_tool` — a
``str | None`` the adapter resolves once — makes the mistake unavailable rather
than merely fixed here. **This is the one deliberate behaviour change in the
policy migrations**, pinned by ``test/policy/test_interrupt_patch_parity.py``,
which asserts the old side raises and the new side does not.
"""

from __future__ import annotations

import logging
from typing import Iterable

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ToolCall, ToolDecision

logger = logging.getLogger("yuyutsava.async_subagents.interrupt_middleware")

_INTERRUPT_TOOLS = frozenset({"update_async_task", "cancel_async_task"})


class AsyncTaskInterruptPatchPolicy(Policy):
    """Patch pending tool calls on a background thread before interrupting it.

    Parameters
    ----------
    agent_specs:
        Iterable of ``AsyncSubAgent``-shaped dicts (``name``, ``url``, optional
        ``headers``). The master already builds these for ``create_deep_agent``;
        pass the same list.
    """

    name = "AsyncTaskInterruptPatchPolicy"

    def __init__(self, agent_specs: Iterable[dict]) -> None:
        super().__init__()
        from yuyutsava.async_subagents.interrupt_middleware import RemoteThreadPatcher

        # The SDK plumbing is unchanged and still lives beside the helpers the
        # watcher shares. Holding it rather than reimplementing it keeps one
        # copy of the settle/patch logic.
        self._patcher = RemoteThreadPatcher(agent_specs)

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        if call.resolved_tool not in _INTERRUPT_TOOLS:
            return None
        tracked = self._tracked(call)
        if tracked is not None:
            await self._patcher.patch_pending(
                tracked["agent_name"], tracked["thread_id"])
        return None  # never refuses; the patch is a side effect

    def _tracked(self, call: ToolCall) -> dict | None:
        """The tracked AsyncTask record for this call, from agent state."""
        task_id = (call.args.get("task_id") or "").strip()
        if not task_id:
            return None
        tasks = call.state.get("async_tasks") if isinstance(call.state, dict) else None
        if not tasks:
            return None
        tracked = tasks.get(task_id)
        if not tracked:
            return None
        agent_name = tracked.get("agent_name") if isinstance(tracked, dict) else None
        thread_id = tracked.get("thread_id") if isinstance(tracked, dict) else None
        if not agent_name or not thread_id:
            return None
        if not self._patcher.knows(agent_name):
            logger.debug("Unknown async subagent %r; skipping interrupt patch", agent_name)
            return None
        return {"agent_name": agent_name, "thread_id": thread_id}


__all__ = ["AsyncTaskInterruptPatchPolicy"]
