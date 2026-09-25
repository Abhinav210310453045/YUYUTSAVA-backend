"""Watching a host and saying what changed, without deciding what to do.

Discovery and ingestion are here because they are observations: threads appear
on the host, and the ones still running become mirrored tasks. So is progress
reporting, which is the same act at a finer grain — what has this task done
since the last cycle.

:meth:`TaskPoller.classify` deliberately *reports* rather than acts. A run that
has stopped may mean four different things, and telling them apart needs the
run and the thread together: ``interrupt()`` leaves the run reading ``success``
(it reached an interrupt boundary cleanly) while the thread reads
``interrupted``, so a poller that only looked at the run would report a
completed task that is really waiting for a person. Which unit handles the
verdict is the watcher's decision, not this one's.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from yuyutsava.async_subagents.mirror import MirroredTask
from yuyutsava.async_subagents.watcher import parsing
from yuyutsava.async_subagents.watcher.context import WatcherContext
from yuyutsava.ports.channels import (
    AsyncTaskProgressPayload,
    AsyncTaskStartedPayload,
    ChannelEvent,
)

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


@dataclass(frozen=True)
class Verdict:
    """What a poll found, for the watcher to route."""

    kind: str  # "timeout" | "interrupted" | "terminal"
    run: dict | None = None
    run_id: str | None = None
    status: str = ""
    thread: Any | None = None


class TaskPoller:
    """Finds new tasks, and reports what changed about the ones it knows."""

    def __init__(self, ctx: WatcherContext) -> None:
        self._ctx = ctx

    async def discover_new_threads(self) -> None:
        """List threads on the server and ingest any we don't know yet."""
        assert self._ctx.client is not None
        try:
            threads = await self._ctx.client.threads.search(limit=50)
        except Exception:
            logger.debug("threads.search failed (continuing)", exc_info=True)
            return
        for th in threads or []:
            tid = parsing.extract_thread_id(th)
            if not tid or tid in self._ctx.known_threads:
                continue
            self._ctx.known_threads.add(tid)
            await self._ingest_thread_runs(tid)

    async def _ingest_thread_runs(self, thread_id: str) -> None:
        """For a freshly-discovered thread, mirror only its *still-running* runs.

        Threads whose latest run is already terminal are tasks from a previous
        session that have finished — we mark them known (done in the caller) but
        do NOT re-announce them as ``[bg started] (discovered)`` on every boot.
        Only genuinely in-flight tasks are surfaced/tracked, so the orchestrator
        and CLI still know about background work that is actually still running.
        """
        assert self._ctx.client is not None
        try:
            runs = await self._ctx.client.runs.list(thread_id=thread_id, limit=5)
        except Exception:
            return
        # The latest run (newest-first) decides whether this thread is live.
        latest = parsing.first_run_status(runs)
        if latest not in parsing.NON_TERMINAL:
            return
        for r in runs or []:
            run_dict = r if isinstance(r, dict) else r.__dict__
            run_id = run_dict.get("run_id")
            status = run_dict.get("status") or "running"
            if not run_id:
                continue
            # Use thread_id as the task_id (matches deepagents'
            # AsyncSubAgentMiddleware convention — see middleware/async_subagents.py).
            task_id = thread_id
            if self._ctx.mirror.get(task_id) is not None:
                continue
            # We don't know the agent_name from runs.list alone; try thread
            # metadata, then fall back to the run's assistant/graph id (e.g.
            # "general-purpose" / "file-organizer") instead of an opaque label.
            agent_name = (
                await self._guess_agent_name(thread_id, run_dict)
                or parsing.clean_agent_name(run_dict.get("assistant_id"))
                or "background task"
            )
            now = time.time()
            # Link back to the launching conversation when the orchestrator
            # recorded it (improves bg-ask routing + lets us wake the master on
            # the original thread at completion).
            rec = self._ctx.launch_index.get(task_id) if self._ctx.launch_index else None
            await self._ctx.mirror.upsert(
                MirroredTask(
                    task_id=task_id,
                    agent_name=agent_name,
                    graph_id=run_dict.get("assistant_id") or "",
                    instruction="(discovered)",
                    status=status,
                    started_at=now,
                    last_update_at=now,
                    sub_thread_id=thread_id,
                    parent_thread_id=rec.parent_thread_id if rec else None,
                    origin=rec.origin if rec else None,
                )
            )
            await self._ctx.emit(
                ChannelEvent(
                    payload=AsyncTaskStartedPayload(
                        task_id=task_id,
                        agent_name=agent_name,
                        instruction_preview="(discovered)",
                        ts=now,
                    )
                )
            )

    async def _guess_agent_name(self, thread_id: str, run_dict: dict) -> str | None:
        # Try thread metadata first; some workflows stash agent name there.
        try:
            assert self._ctx.client is not None
            th = await self._ctx.client.threads.get(thread_id=thread_id)
            md = th.get("metadata") if isinstance(th, dict) else getattr(th, "metadata", None)
            if isinstance(md, dict):
                hint = md.get("agent_name") or md.get("assistant_name")
                if hint:
                    return str(hint)
        except Exception:  # noqa: BLE001 — a name is a nicety; the caller has a fallback
            logger.debug("reading metadata of thread %s failed", thread_id, exc_info=True)
        return None

    async def emit_progress(self, task: MirroredTask) -> None:
        """Stream any new subagent steps as ``AsyncTaskProgressPayload`` events.

        Polls the sub-thread's message history and emits one progress event per
        new tool call / assistant-text step since the last cycle, so background
        subagents report step-by-step through the same channel the foreground
        task uses. Best-effort: failures are logged and skipped.
        """
        assert self._ctx.client is not None
        if not task.sub_thread_id:
            return
        try:
            thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
        except Exception:
            logger.debug("threads.get for progress failed for %s", task.task_id, exc_info=True)
            return
        values = (
            thread.get("values") if isinstance(thread, dict) else getattr(thread, "values", None)
        )
        seen = self._ctx.progress_seen.get(task.task_id, 0)
        steps, total = parsing.new_progress_steps(values, seen)
        if total == seen:
            return
        self._ctx.progress_seen[task.task_id] = total
        for kind_hint, text in steps:
            await self._ctx.emit(
                ChannelEvent(
                    payload=AsyncTaskProgressPayload(
                        task_id=task.task_id,
                        agent_name=task.agent_name,
                        kind_hint=kind_hint,
                        text=text,
                        ts=time.time(),
                    )
                )
            )

    async def get_task_transcript(self, task_id: str) -> list[dict] | None:
        """Return a background task's full message transcript, or None.

        Looks up the task in the mirror, fetches its sub-thread state from the
        langgraph host, and flattens ``messages`` into UI-friendly rows. Returns
        ``None`` when the task is unknown; ``[]`` when it has no messages yet.
        """
        if self._ctx.client is None:
            return None
        task = self._ctx.mirror.get(task_id)
        if task is None or not task.sub_thread_id:
            return None
        try:
            thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
        except Exception:
            logger.debug("get_task_transcript threads.get failed for %s", task_id, exc_info=True)
            return []
        values = (
            thread.get("values") if isinstance(thread, dict) else getattr(thread, "values", None)
        )
        return parsing.transcript_rows(values)

    async def classify(self, task: MirroredTask) -> Verdict | None:
        """What this task needs, or ``None`` when it needs nothing.

        A still-running task is handled here — its status is recorded and its
        new steps reported — because neither is a decision. Everything that is
        goes back to the caller as a verdict.
        """
        if not task.sub_thread_id:
            return None
        if (time.time() - task.started_at) > self._ctx.task_timeout:
            return Verdict(kind="timeout")
        run = await self._latest_run(task)
        if run is None:
            return None
        run_status = run.get("status") or task.status
        if run_status in parsing.NON_TERMINAL:
            if task.status != run_status:
                await self._ctx.mirror.set_status(task.task_id, run_status)
            await self.emit_progress(task)
            return None

        # The run stopped. Only the thread says whether that is an outcome or a
        # pause, so it has to be fetched before the verdict is made.
        try:
            thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
        except Exception:
            logger.debug("threads.get failed for %s", task.task_id, exc_info=True)
            return None
        thread_status = (
            thread.get("status") if isinstance(thread, dict) else getattr(thread, "status", None)
        ) or ""
        if thread_status == "interrupted":
            return Verdict(kind="interrupted", run_id=run.get("run_id"), thread=thread)
        if run_status in parsing.TERMINAL_STATUSES or run_status in ("success", "error"):
            return Verdict(kind="terminal", run=run, status=run_status, thread=thread)

        # Unknown status: record it and look again next cycle.
        await self._ctx.mirror.set_status(task.task_id, run_status)
        return None

    async def _latest_run(self, task: MirroredTask) -> dict | None:
        """The task's most recent run as a dict, or ``None`` if unreadable."""
        try:
            runs = await self._ctx.client.runs.list(thread_id=task.sub_thread_id, limit=1)
        except Exception:
            logger.debug("runs.list failed for %s", task.task_id, exc_info=True)
            return None
        if not runs:
            return None
        return runs[0] if isinstance(runs[0], dict) else runs[0].__dict__


__all__ = ["TaskPoller", "Verdict"]
