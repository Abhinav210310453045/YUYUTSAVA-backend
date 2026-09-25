"""Routing a paused run's questions to a person, and resuming it.

A background subagent that hits ``interrupt()`` stops and waits. Nothing else
will move it: the run has reached an interrupt boundary cleanly, so its status
reads ``success`` while the thread's reads ``interrupted``, and only a new run
carrying the replies restarts it.

The parts that matter, each of which has cost a hang:

* resume is keyed by interrupt id — LangGraph refuses a bare value once more
  than one interrupt is pending;
* an interrupt is asked once, because resume is not instant and the same
  interrupt is still there next cycle;
* an ask surface that raises is a refusal, never consent;
* a resume that fails finishes the task, rather than leaving it awaiting a user
  who has already answered.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from yuyutsava.async_subagents.mirror import MirroredTask
from yuyutsava.async_subagents.watcher.context import WatcherContext
from yuyutsava.policy.interrupt_format import (
    body_for_interrupt,
    options_for_interrupt,
    title_for_interrupt,
)
from yuyutsava.ports.channels import (
    AskPrompt,
    AsyncTaskAwaitingUserPayload,
    AsyncTaskCompletedPayload,
    AsyncTaskProgressPayload,
    ChannelEvent,
)

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


class InterruptHandler:
    """Asks the user what a paused run should do, then restarts it."""

    def __init__(self, ctx: WatcherContext) -> None:
        self._ctx = ctx

    async def handle(
        self,
        task: MirroredTask,
        run_id: str | None,
        thread: Any | None = None,
    ) -> None:
        """Route every fresh ``Interrupt`` to the user and resume the run.

        deepagents/langgraph allow multiple Interrupts in flight; we route
        them sequentially. ``_handled_interrupts`` prevents re-asking the
        same interrupt across slow resume cycles.

        When the caller has already fetched the thread object, pass it in
        to save a second roundtrip.
        """
        assert self._ctx.client is not None
        if thread is None:
            try:
                thread = await self._ctx.client.threads.get(thread_id=task.sub_thread_id)
            except Exception:
                logger.exception("threads.get failed during interrupt handling")
                return
        pending = self._fresh_interrupts(task, thread)
        if not pending:
            # Status says interrupted but no fresh interrupts — record state
            # and try again next cycle.
            await self._ctx.mirror.set_status(task.task_id, "interrupted")
            return
        replies = await self._ask_each(task, pending)
        await self._resume_with(task, replies)

    def _fresh_interrupts(self, task: MirroredTask, thread: Any) -> list[tuple[str, Any]]:
        """``(interrupt_id, value)`` for each interrupt not yet asked about."""
        if isinstance(thread, dict):
            interrupts_map = thread.get("interrupts") or {}
        else:
            interrupts_map = getattr(thread, "interrupts", {}) or {}
        already = self._ctx.handled_interrupts.setdefault(task.task_id, set())
        pending: list[tuple[str, Any]] = []
        for _task_id_in_thread, ilist in (
            interrupts_map.items() if isinstance(interrupts_map, dict) else []
        ):
            for it in ilist or []:
                it_id = it.get("id") if isinstance(it, dict) else getattr(it, "id", None)
                if not it_id or it_id in already:
                    continue
                value = it.get("value") if isinstance(it, dict) else getattr(it, "value", {})
                pending.append((it_id, value))
        return pending

    async def _ask_each(self, task: MirroredTask, pending: list[tuple[str, Any]]) -> dict[str, str]:
        """Ask about each interrupt in turn; an ask that raises is a rejection."""
        already = self._ctx.handled_interrupts.setdefault(task.task_id, set())
        replies: dict[str, str] = {}
        for it_id, value in pending:
            ask_id = str(uuid.uuid4())
            ask = self._build_ask(task, ask_id, it_id, value)
            await self._ctx.mirror.set_status(task.task_id, "awaiting_user", pending_ask_id=ask_id)
            await self._ctx.emit(
                ChannelEvent(
                    payload=AsyncTaskAwaitingUserPayload(
                        task_id=task.task_id,
                        agent_name=task.agent_name,
                        ask_id=ask_id,
                        title=ask.title,
                        ts=time.time(),
                    )
                )
            )
            try:
                reply = await self._ctx.ask(ask)
            except Exception:
                logger.exception("ask_handler raised; auto-rejecting")
                reply = "reject"
            already.add(it_id)
            replies[it_id] = reply
        return replies

    async def _resume_with(self, task: MirroredTask, replies: dict[str, str]) -> None:
        """Resume the run ONCE with every reply, keyed by interrupt id.

        LangGraph requires the keyed form whenever more than one interrupt is
        pending (a bare ``{"resume": value}`` raises "you must specify the
        interrupt id when resuming"). A resume that fails finishes the task
        rather than leaving it awaiting a user who has already answered.
        """
        try:
            await self._ctx.client.runs.create(
                thread_id=task.sub_thread_id,
                assistant_id=task.graph_id,
                command={"resume": replies},
                multitask_strategy="interrupt",
            )
        except Exception:
            logger.exception("runs.create(resume=...) failed for %s", task.task_id)
            updated = await self._ctx.mirror.set_status(
                task.task_id, "error", error="resume_failed"
            )
            await self._ctx.emit(
                ChannelEvent(
                    payload=AsyncTaskCompletedPayload(
                        task_id=task.task_id,
                        agent_name=task.agent_name,
                        ok=False,
                        summary="resume_failed",
                        duration_sec=time.time() - task.started_at,
                        ts=time.time(),
                    )
                )
            )
            await self._ctx.notify_complete(updated or task, False, "resume_failed")
            return
        await self._ctx.mirror.set_status(task.task_id, "running", pending_ask_id=None)
        n = len(replies)
        await self._ctx.emit(
            ChannelEvent(
                payload=AsyncTaskProgressPayload(
                    task_id=task.task_id,
                    agent_name=task.agent_name,
                    kind_hint="resumed",
                    text=f"resumed after {n} user repl{'y' if n == 1 else 'ies'}",
                    ts=time.time(),
                )
            )
        )

    async def resume(self, task_id: str, interrupt_id: str | None, reply: str) -> bool:
        """Push a reply into a task parked on an interrupt. Returns success.

        The restart path: an ask raised before the daemon went down has no
        in-memory waiter left, but the run itself is still checkpointed and
        interrupted on the subagent host. Answering it means re-entering the
        run directly rather than waking a future that is already gone.
        """
        task = self._ctx.mirror.get(task_id)
        if task is None:
            logger.warning("resume_interrupt: unknown task %s", task_id)
            return False
        if not interrupt_id:
            logger.warning(
                "resume_interrupt: task %s has no interrupt id — cannot route the reply",
                task_id,
            )
            return False
        try:
            await self._ctx.client.runs.create(
                thread_id=task.sub_thread_id,
                assistant_id=task.graph_id,
                command={"resume": {interrupt_id: reply}},
                multitask_strategy="interrupt",
            )
        except Exception:  # noqa: BLE001
            logger.exception("resume_interrupt: runs.create failed for %s", task_id)
            return False
        await self._ctx.mirror.set_status(task_id, "running", pending_ask_id=None)
        logger.info("resume_interrupt: task %s resumed with a stored reply", task_id)
        return True

    def _build_ask(
        self,
        task: MirroredTask,
        ask_id: str,
        interrupt_id: str,
        value: Any,
    ) -> AskPrompt:
        # Format identically to foreground asks via the shared formatter, so the
        # CLI prompt and the UI AskCard get a clean title/body (no raw-JSON blob).
        # Preserve the interrupt's real ``type`` (task_runner_permission /
        # permission_request / user_question) so the formatter dispatches right —
        # only wrap as a free-text question when the value isn't a dict.
        if isinstance(value, dict):
            iv: dict[str, Any] = dict(value)
        else:
            iv = {"type": "user_question", "question": str(value)}
        # Carry interrupt_id so consumers can correlate if they want.
        iv["interrupt_id"] = interrupt_id
        iv.setdefault("agent_path", f"{self._ctx.agent_path_root}/{task.agent_name}#bg")
        return AskPrompt(
            ask_id=ask_id,
            title=title_for_interrupt(iv),
            body=body_for_interrupt(iv),
            options=options_for_interrupt(iv),
            interrupt_value=iv,
            session_id=task.parent_thread_id or task.task_id,
            agent_path=iv["agent_path"],
            # A background ask belongs to the task, not to whichever chat
            # happened to launch it: it renders in the Inbox (and the overlay),
            # never inline inside that conversation. task_id + interrupt_id are
            # what let the resume path re-enter the right run afterwards.
            surface="background",
            thread_id=task.parent_thread_id,
            task_id=task.task_id,
            agent_label=task.agent_name,
            interrupt_id=interrupt_id,
        )


__all__ = ["InterruptHandler"]
