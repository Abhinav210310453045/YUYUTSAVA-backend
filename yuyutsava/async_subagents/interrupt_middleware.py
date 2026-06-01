"""Patch pending tool_calls on the subagent thread before interrupting it.

``update_async_task`` and ``cancel_async_task`` call
``client.runs.create(..., multitask_strategy="interrupt")`` on the subagent
thread. If the subagent had an in-flight tool call when interrupted, its
thread state ends up with a dangling ``AIMessage(tool_calls=[...])`` and no
matching ``ToolMessage``. The next agent tick fails inside
``_validate_chat_history`` with::

    Found AIMessages with tool_calls that do not have a corresponding ToolMessage.

This middleware intercepts those two tools on the *master* side and, before
the handler runs, POSTs synthetic ``ToolMessage``s into the subagent thread
for every unresolved ``tool_call_id``. The subsequent interrupt + new run
then sees a clean message list and validates fine.

Wire it AFTER ``BackgroundTaskCapMiddleware`` in the master's middleware list
so the cap check (which can short-circuit and return a refusal ToolMessage)
runs first; this middleware only does work when the tool actually proceeds.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger("yuyutsava.async_subagents.interrupt_middleware")

_INTERRUPT_TOOLS = frozenset({"update_async_task", "cancel_async_task"})


class AsyncTaskInterruptPatchMiddleware(AgentMiddleware):
    """Inject synthetic ToolMessages for pending tool_calls before an interrupt.

    Parameters
    ----------
    agent_specs:
        Iterable of ``AsyncSubAgent``-shaped dicts (``name``, ``url``,
        optional ``headers``). The master agent already builds these to pass
        to ``create_deep_agent``; pass the same list here.
    """

    def __init__(self, agent_specs: Iterable[dict]) -> None:
        super().__init__()
        self._url_by_name: dict[str, str | None] = {}
        self._headers_by_name: dict[str, dict[str, str]] = {}
        for spec in agent_specs:
            name = spec.get("name") if isinstance(spec, dict) else None
            if not name:
                continue
            self._url_by_name[name] = spec.get("url")
            self._headers_by_name[name] = dict(spec.get("headers") or {})
        self._sync_clients: dict[str, Any] = {}
        self._async_clients: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        if request.tool.name in _INTERRUPT_TOOLS:
            tracked = self._lookup_tracked(request)
            if tracked is not None:
                self._patch_pending_sync(tracked["agent_name"], tracked["thread_id"])
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        if request.tool.name in _INTERRUPT_TOOLS:
            tracked = self._lookup_tracked(request)
            if tracked is not None:
                await self._patch_pending_async(tracked["agent_name"], tracked["thread_id"])
        return await handler(request)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lookup_tracked(self, request: Any) -> dict | None:
        """Pull the tracked AsyncTask record for this tool call out of state."""
        args = (request.tool_call or {}).get("args") or {}
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return None
        state = getattr(request, "state", None) or {}
        tasks = state.get("async_tasks") if isinstance(state, dict) else None
        if not tasks:
            return None
        tracked = tasks.get(task_id)
        if not tracked:
            return None
        agent_name = tracked.get("agent_name") if isinstance(tracked, dict) else None
        thread_id = tracked.get("thread_id") if isinstance(tracked, dict) else None
        if not agent_name or not thread_id:
            return None
        if agent_name not in self._url_by_name:
            logger.debug("Unknown async subagent %r; skipping interrupt patch", agent_name)
            return None
        return {"agent_name": agent_name, "thread_id": thread_id}

    def _resolve_headers(self, agent_name: str) -> dict[str, str]:
        headers = dict(self._headers_by_name.get(agent_name) or {})
        if "x-auth-scheme" not in headers:
            headers["x-auth-scheme"] = "langsmith"
        return headers

    def _get_sync_client(self, agent_name: str):
        if agent_name not in self._sync_clients:
            from langgraph_sdk import get_sync_client
            self._sync_clients[agent_name] = get_sync_client(
                url=self._url_by_name.get(agent_name),
                headers=self._resolve_headers(agent_name),
            )
        return self._sync_clients[agent_name]

    def _get_async_client(self, agent_name: str):
        if agent_name not in self._async_clients:
            from langgraph_sdk import get_client
            self._async_clients[agent_name] = get_client(
                url=self._url_by_name.get(agent_name),
                headers=self._resolve_headers(agent_name),
            )
        return self._async_clients[agent_name]

    @staticmethod
    def _find_pending_tool_calls(messages: list[Any]) -> list[tuple[str, str]]:
        """Return ``[(tool_call_id, tool_name)]`` for unresolved tool_calls.

        Walks messages in order; an AIMessage's tool_call is "resolved" by any
        later ToolMessage with the matching ``tool_call_id``.
        """
        pending: dict[str, str] = {}
        for m in messages:
            mtype = _msg_type(m)
            if mtype == "ai":
                for tc in _ai_tool_calls(m):
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    tc_name = (
                        tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    )
                    if tc_id:
                        pending[tc_id] = tc_name or ""
            elif mtype == "tool":
                tc_id = (
                    m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
                )
                if tc_id:
                    pending.pop(tc_id, None)
        return list(pending.items())

    @staticmethod
    def _build_synthetic(pending: list[tuple[str, str]]) -> list[dict]:
        return [
            {
                "type": "tool",
                "tool_call_id": tc_id,
                "name": tc_name or "tool",
                "content": "[interrupted by user update — tool was cancelled mid-run]",
                "status": "error",
            }
            for tc_id, tc_name in pending
        ]

    def _patch_pending_sync(self, agent_name: str, thread_id: str) -> int:
        try:
            client = self._get_sync_client(agent_name)
            thread = client.threads.get(thread_id=thread_id)
        except Exception as e:  # noqa: BLE001  # SDK raises untyped
            logger.warning("interrupt-patch: cannot fetch thread %s: %s", thread_id, e)
            return 0
        messages = (thread.get("values") or {}).get("messages") or []
        pending = self._find_pending_tool_calls(messages)
        if not pending:
            return 0
        try:
            client.threads.update_state(
                thread_id=thread_id,
                values={"messages": self._build_synthetic(pending)},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt-patch: cannot update thread %s: %s", thread_id, e)
            return 0
        logger.info(
            "interrupt-patch: injected %d synthetic ToolMessage(s) on thread %s",
            len(pending), thread_id,
        )
        return len(pending)

    async def _patch_pending_async(self, agent_name: str, thread_id: str) -> int:
        try:
            client = self._get_async_client(agent_name)
            thread = await client.threads.get(thread_id=thread_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt-patch: cannot fetch thread %s: %s", thread_id, e)
            return 0
        messages = (thread.get("values") or {}).get("messages") or []
        pending = self._find_pending_tool_calls(messages)
        if not pending:
            return 0
        try:
            await client.threads.update_state(
                thread_id=thread_id,
                values={"messages": self._build_synthetic(pending)},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt-patch: cannot update thread %s: %s", thread_id, e)
            return 0
        logger.info(
            "interrupt-patch: injected %d synthetic ToolMessage(s) on thread %s",
            len(pending), thread_id,
        )
        return len(pending)


def _msg_type(m: Any) -> str | None:
    if isinstance(m, dict):
        t = m.get("type") or m.get("role")
        if t == "assistant":
            return "ai"
        if t == "user":
            return "human"
        return t
    return getattr(m, "type", None)


def _ai_tool_calls(m: Any) -> list[Any]:
    if isinstance(m, dict):
        return list(m.get("tool_calls") or [])
    return list(getattr(m, "tool_calls", None) or [])
