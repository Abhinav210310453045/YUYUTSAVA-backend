"""Patch pending tool_calls on the subagent thread before interrupting it.

``update_async_task`` and ``cancel_async_task`` call
``client.runs.create(..., multitask_strategy="interrupt")`` on the subagent
thread. If the subagent had an in-flight tool call when interrupted, its
thread state ends up with a dangling ``AIMessage(tool_calls=[...])`` and no
matching ``ToolMessage``. The next agent tick fails inside
``_validate_chat_history`` with::

    Found AIMessages with tool_calls that do not have a corresponding ToolMessage.

:class:`RemoteThreadPatcher` POSTs synthetic ``ToolMessage``s into the subagent
thread for every unresolved ``tool_call_id``, so the subsequent interrupt + new
run sees a clean message list and validates fine.

Since Phase 4 this module holds no middleware. The *decision* — which tool calls
warrant a patch, and against which thread — is
:class:`~yuyutsava.async_subagents.interrupt_patch_policy.AsyncTaskInterruptPatchPolicy`.
What stays here is the SDK client that carries it out, plus
``find_pending_tool_calls`` / ``build_synthetic_toolmessages``, which the
watcher's self-heal shares so both agree on what "orphaned" means.

Order still matters: the policy runs AFTER ``BackgroundTaskCapPolicy``, so the
cap (which can short-circuit with a refusal) is evaluated first and no patch is
attempted for a launch that never happens.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

logger = logging.getLogger("yuyutsava.async_subagents.interrupt_middleware")

_INTERRUPT_TOOLS = frozenset({"update_async_task", "cancel_async_task"})

# Content used for synthetic ToolMessages that heal an orphaned tool call.
SYNTHETIC_TOOL_CONTENT = "[interrupted by user update — tool was cancelled mid-run]"

# Run statuses that mean the run is no longer executing (so a dangling
# tool_call, if any, is now committed to thread state and safe to patch).
_SETTLED_RUN_STATUSES = frozenset({"success", "error", "interrupted", "cancelled", "timeout"})


class RemoteThreadPatcher:
    """Talks to a background subagent's Agent Protocol server to heal its thread.

    Split out of ``AsyncTaskInterruptPatchMiddleware`` in Phase 4: none of this is
    policy. It is an SDK client that settles a run and injects synthetic
    ToolMessages, and it is shared verbatim between the middleware (until it is
    deleted) and ``AsyncTaskInterruptPatchPolicy``. Holding it, rather than
    reaching into a middleware's private members, is what keeps one copy of the
    settle/patch logic.

    Parameters
    ----------
    agent_specs:
        Iterable of ``AsyncSubAgent``-shaped dicts (``name``, ``url``,
        optional ``headers``). The master agent already builds these to pass
        to ``create_deep_agent``; pass the same list here.
    """

    def __init__(self, agent_specs: Iterable[dict]) -> None:
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

    def knows(self, agent_name: str) -> bool:
        """Whether *agent_name* is one of the subagents this master launched."""
        return agent_name in self._url_by_name

    async def patch_pending(self, agent_name: str, thread_id: str) -> int:
        """Settle the active run and resolve orphaned tool calls. Returns how many."""
        return await self._patch_pending_async(agent_name, thread_id)

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

    def _settle_active_run_sync(self, client: Any, thread_id: str) -> None:
        """Cancel the latest in-flight run and wait until it settles.

        Without this, a tool call still executing when we read thread state is
        not yet persisted, so ``find_pending_tool_calls`` sees nothing — the
        interrupt then orphans it *after* our patch. Cancelling first and
        waiting for the run to leave the executing state forces the dangling
        ``AIMessage(tool_calls)`` to be committed so the patch below catches it.
        """
        try:
            runs = client.runs.list(thread_id=thread_id, limit=1)
        except Exception:  # noqa: BLE001
            return
        run = _first_run(runs)
        if run is None:
            return
        status = run.get("status") or ""
        run_id = run.get("run_id")
        if status in _SETTLED_RUN_STATUSES or not run_id:
            return
        try:
            client.runs.cancel(thread_id=thread_id, run_id=run_id)
        except Exception:  # noqa: BLE001
            pass
        # Poll briefly for the run to settle (cap ~3s).
        for _ in range(15):
            time.sleep(0.2)
            try:
                cur = client.runs.get(thread_id=thread_id, run_id=run_id)
            except Exception:  # noqa: BLE001
                break
            if (cur.get("status") or "") in _SETTLED_RUN_STATUSES:
                break

    async def _settle_active_run_async(self, client: Any, thread_id: str) -> None:
        import asyncio
        try:
            runs = await client.runs.list(thread_id=thread_id, limit=1)
        except Exception:  # noqa: BLE001
            return
        run = _first_run(runs)
        if run is None:
            return
        status = run.get("status") or ""
        run_id = run.get("run_id")
        if status in _SETTLED_RUN_STATUSES or not run_id:
            return
        try:
            await client.runs.cancel(thread_id=thread_id, run_id=run_id)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(15):
            await asyncio.sleep(0.2)
            try:
                cur = await client.runs.get(thread_id=thread_id, run_id=run_id)
            except Exception:  # noqa: BLE001
                break
            if (cur.get("status") or "") in _SETTLED_RUN_STATUSES:
                break

    def _patch_pending_sync(self, agent_name: str, thread_id: str) -> int:
        try:
            client = self._get_sync_client(agent_name)
            self._settle_active_run_sync(client, thread_id)
            thread = client.threads.get(thread_id=thread_id)
        except Exception as e:  # noqa: BLE001  # SDK raises untyped
            logger.warning("interrupt-patch: cannot fetch thread %s: %s", thread_id, e)
            return 0
        messages = (thread.get("values") or {}).get("messages") or []
        pending = find_pending_tool_calls(messages)
        if not pending:
            return 0
        try:
            client.threads.update_state(
                thread_id=thread_id,
                values={"messages": build_synthetic_toolmessages(pending)},
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
            await self._settle_active_run_async(client, thread_id)
            thread = await client.threads.get(thread_id=thread_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt-patch: cannot fetch thread %s: %s", thread_id, e)
            return 0
        messages = (thread.get("values") or {}).get("messages") or []
        pending = find_pending_tool_calls(messages)
        if not pending:
            return 0
        try:
            await client.threads.update_state(
                thread_id=thread_id,
                values={"messages": build_synthetic_toolmessages(pending)},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt-patch: cannot update thread %s: %s", thread_id, e)
            return 0
        logger.info(
            "interrupt-patch: injected %d synthetic ToolMessage(s) on thread %s",
            len(pending), thread_id,
        )
        return len(pending)


def _first_run(runs: Any) -> dict | None:
    """Normalize ``runs.list`` output to the first run as a dict, or None."""
    if not runs:
        return None
    r = runs[0]
    if isinstance(r, dict):
        return r
    return getattr(r, "__dict__", None)


def find_pending_tool_calls(messages: list[Any]) -> list[tuple[str, str]]:
    """Return ``[(tool_call_id, tool_name)]`` for unresolved tool_calls.

    Walks messages in order; an AIMessage's tool_call is "resolved" by any
    later ToolMessage with the matching ``tool_call_id``. Shared by the master
    interrupt-patch middleware and the watcher's self-heal so both agree on
    what "orphaned" means.
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


def build_synthetic_toolmessages(
    pending: list[tuple[str, str]],
    content: str = SYNTHETIC_TOOL_CONTENT,
) -> list[dict]:
    """Build synthetic error ToolMessages that resolve orphaned tool calls."""
    return [
        {
            "type": "tool",
            "tool_call_id": tc_id,
            "name": tc_name or "tool",
            "content": content,
            "status": "error",
        }
        for tc_id, tc_name in pending
    ]


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
