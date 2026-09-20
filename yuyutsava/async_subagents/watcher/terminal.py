"""Deciding what a finished run actually says, and telling everyone once.

A terminal run is not self-describing. Success means reading the last message
off the thread; error means hunting the detail across three places, because
``runs.list`` usually omits it, ``runs.get`` usually carries it, and a genuine
tool failure is sometimes only recoverable from the thread's own messages.

When none of them has anything, this says so plainly rather than surfacing the
completion summary as the "error" — the launching agent acts on whatever it is
told, so an unrelated success payload presented as a failure is worse than
"no detail".

The timeout path ends here too, because a task that ran out of wall clock is
finished by the same rules: cancel what is still running, record the verdict,
emit it, and notify exactly once.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from yuyutsava.async_subagents.interrupt_middleware import (
    build_synthetic_toolmessages,
    find_pending_tool_calls,
)
from yuyutsava.async_subagents.mirror import MirroredTask
from yuyutsava.async_subagents.watcher import parsing
from yuyutsava.async_subagents.watcher.context import WatcherContext
from yuyutsava.ports.channels import AsyncTaskCompletedPayload, ChannelEvent

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


class TerminalReporter:
    """Turns a finished run into a verdict, an event and one notification."""

    def __init__(self, ctx: WatcherContext) -> None:
        self._ctx = ctx

    async def report(self, task: MirroredTask, run: dict, status: str) -> None:
        assert self._ctx.client is not None
        summary = ""
        error_text: str | None = None
        if status == "success":
            try:
                thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
                values = (
                    thread.get("values")
                    if isinstance(thread, dict)
                    else getattr(thread, "values", None)
                )
                summary = parsing.last_message_text(values)[:400]
            except Exception:
                logger.debug("threads.get for terminal summary failed", exc_info=True)
                summary = "(completed with no readable output)"
        elif status == "error":
            raw = parsing.run_error_text(run)
            if not raw:
                # The list-view run object often omits the error detail; the full
                # run record usually carries it (e.g. langgraph-api's generic
                # "An internal error occurred").
                raw = await self._fetch_run_error(task, run)
            if not raw:
                # Still nothing — recover a genuine tool failure from the thread.
                try:
                    thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
                    values = (
                        thread.get("values")
                        if isinstance(thread, dict)
                        else getattr(thread, "values", None)
                    )
                    raw = parsing.error_text_from_thread(values)
                except Exception:
                    logger.debug("threads.get for terminal error failed", exc_info=True)
            if not raw:
                # No detail anywhere: say so plainly rather than surfacing an
                # unrelated success payload or completion summary as the "error".
                raw = (
                    "run ended in error status with no error detail "
                    "(likely an internal langgraph-api run failure); see task logs"
                )
            # Compact so it never bloats the launching agent's context; the full
            # text remains available via GET /tasks/{id}/logs.
            error_text = parsing.compact_error(raw)
            summary = error_text
        else:  # cancelled / timeout
            summary = status

        ok = status == "success"
        duration = time.time() - task.started_at
        self._ctx.progress_seen.pop(task.task_id, None)
        updated = await self._ctx.mirror.set_status(
            task.task_id,
            status,
            summary=summary,
            error=error_text,
        )
        await self._ctx.emit(
            ChannelEvent(
                payload=AsyncTaskCompletedPayload(
                    task_id=task.task_id,
                    agent_name=task.agent_name,
                    ok=ok,
                    summary=summary[:300],
                    duration_sec=duration,
                    ts=time.time(),
                )
            )
        )
        await self._ctx.notify_complete(updated or task, ok, summary)

    async def _fetch_run_error(self, task: MirroredTask, run: Any) -> str:
        """Pull the error detail from the full run record, if the SDK exposes it.

        The run object from ``runs.list`` frequently omits ``error``; fetching the
        run by id often returns the populated field. Best-effort and defensive:
        any failure (or an SDK that never surfaces it) just yields "".
        """
        assert self._ctx.client is not None
        run_id = run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", None)
        if not (task.sub_thread_id and run_id):
            return ""
        try:
            full = await self._ctx.client.runs.get(thread_id=task.sub_thread_id, run_id=run_id)
        except Exception:
            logger.debug("runs.get for terminal error failed for %s", task.task_id, exc_info=True)
            return ""
        return parsing.run_error_text(full)

    async def heal_orphaned_tool_calls(self, task: MirroredTask, thread: Any) -> None:
        """Inject synthetic ToolMessages for any unresolved tool_calls.

        Defensive net for the case the master-side interrupt patch missed: an
        errored thread carrying an ``AIMessage(tool_calls)`` with no matching
        ``ToolMessage`` is otherwise unreadable.

        At most once per thread, checked here as well as by the caller: this is
        the code that owns ``_healed_threads``, and a persistently failing
        thread patched on every cycle is a write loop against the host.
        """
        assert self._ctx.client is not None
        if not task.sub_thread_id or task.sub_thread_id in self._ctx.healed_threads:
            return
        values = (
            thread.get("values") if isinstance(thread, dict) else getattr(thread, "values", None)
        ) or {}
        messages = values.get("messages") if isinstance(values, dict) else None
        pending = find_pending_tool_calls(messages or [])
        if not pending:
            return
        try:
            await self._ctx.client.threads.update_state(
                thread_id=task.sub_thread_id,
                values={"messages": build_synthetic_toolmessages(pending)},
            )
            self._ctx.healed_threads.add(task.sub_thread_id)
            logger.info(
                "watcher self-heal: patched %d orphaned tool_call(s) on thread %s",
                len(pending),
                task.sub_thread_id,
            )
        except Exception:
            logger.debug("watcher self-heal update_state failed", exc_info=True)

    async def enforce_timeout(self, task: MirroredTask) -> None:
        """Cancel a task that exceeded the wall-clock timeout."""
        assert self._ctx.client is not None
        if not task.sub_thread_id:
            return
        try:
            runs = await self._ctx.client.runs.list(thread_id=task.sub_thread_id, limit=1)
            for r in runs or []:
                rid = r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", None)
                if rid:
                    await self._ctx.client.runs.cancel(thread_id=task.sub_thread_id, run_id=rid)
        except Exception:
            logger.debug("timeout cancel failed", exc_info=True)
        self._ctx.progress_seen.pop(task.task_id, None)
        updated = await self._ctx.mirror.set_status(
            task.task_id,
            "timeout",
            error=f"per_task_timeout ({int(self._ctx.task_timeout)}s)",
            summary="task exceeded wall-clock timeout",
        )
        summary = f"timeout after {int(self._ctx.task_timeout)}s"
        await self._ctx.emit(
            ChannelEvent(
                payload=AsyncTaskCompletedPayload(
                    task_id=task.task_id,
                    agent_name=task.agent_name,
                    ok=False,
                    summary=summary,
                    duration_sec=time.time() - task.started_at,
                    ts=time.time(),
                )
            )
        )
        await self._ctx.notify_complete(updated or task, False, summary)


__all__ = ["TerminalReporter"]
