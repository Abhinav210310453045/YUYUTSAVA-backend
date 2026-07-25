"""
Orchestrator outer loop.

Pops ``OrchestratorTask``s off the queue and runs the orchestrator graph
on each. Each task gets a fresh thread_id; the graph + checkpoint are
discarded between tasks. This is the rule that bounds context cost
regardless of daemon uptime.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import uuid

from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.async_subagents.launch_index import parse_async_task_id
from yuyutsava.core.engine import build_orchestrator
from yuyutsava.core.streaming import StreamEvent, astream_agent_iter
from yuyutsava.daemon.channels import (
    AskPrompt,
    ChannelEvent,
    ChannelRouter,
    LogPayload,
    TimelinePayload,
    TokenPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from yuyutsava.llm import model_name_of
from yuyutsava.daemon.usage import UsageContext
from yuyutsava.storage.ids import mint_thread_id as _mint_thread_id
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.storage.events import Store
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger("yuyutsava.daemon.orchestrator_loop")


# Interrupt formatting now lives in a shared, dependency-free module so the
# background AsyncTaskHealthWatcher can reuse the exact same logic (no raw-JSON
# blobs in background asks). Aliased here for backward compatibility.
from yuyutsava.daemon.interrupt_format import (  # noqa: E402
    body_for_interrupt as _body_for_interrupt,
    options_for_interrupt as _options_for_interrupt,
    title_for_interrupt as _title_for_interrupt,
)


def make_ask_handler(
    channels: ChannelRouter,
    *,
    default_session_id: str,
    default_agent_path: str = "orchestrator",
    surface: str = "background",
    default_agent_label: str = "Orchestrator",
    task_id: str | None = None,
):
    """Factory producing the ask handler the orchestrator + bg watcher share.

    Both the master's streaming loop and the ``AsyncTaskHealthWatcher`` route
    interrupt values into ``ChannelRouter.post_ask`` with the same shape.
    Extracting it here keeps the formatting consistent and lets the watcher
    reuse the daemon's HITL surface without duplicating logic.

    ``surface`` tags who owns the resulting ask, which is what decides where it
    may render: an orchestrator/background ask belongs in the Inbox (and the
    overlay), never inline inside somebody's open chat.
    """

    async def ask_handler(interrupt_value: dict) -> str:
        iv = interrupt_value if isinstance(interrupt_value, dict) else {}
        session_id = iv.get("session_id") or default_session_id
        ask = AskPrompt(
            ask_id=str(uuid.uuid4()),
            title=_title_for_interrupt(interrupt_value),
            body=_body_for_interrupt(interrupt_value),
            options=_options_for_interrupt(interrupt_value),
            interrupt_value=dict(iv),
            session_id=session_id,
            agent_path=iv.get("agent_path") or default_agent_path,
            surface=surface,
            thread_id=session_id,
            task_id=iv.get("task_id") or task_id,
            agent_label=iv.get("agent_label") or default_agent_label,
            interrupt_id=iv.get("interrupt_id"),
        )
        return await channels.post_ask(ask)

    return ask_handler


class OrchestratorLoop:
    def __init__(
        self,
        *,
        task_queue: asyncio.Queue[OrchestratorTask],
        channels: ChannelRouter,
        store: Store,
        orchestrator_model: BaseChatModel,
        deps: OrchestratorDeps,
        orchestrator_token_budget: int,
        checkpointer: BaseCheckpointSaver | None = None,
        prefs_injector: object | None = None,  # yuyutsava.prefs.injector.PrefsInjector
        skill_store: object | None = None,  # yuyutsava.skills.store.SkillStore (dual-write)
        task_registry: object | None = None,  # yuyutsava.daemon.task_registry.TaskRegistry
        model_router: object | None = None,  # yuyutsava.core.model_router.ModelRouter
        admission: object | None = None,  # yuyutsava.daemon.resources.AdmissionController
        launch_index: object | None = None,  # yuyutsava.async_subagents.launch_index.LaunchIndex
    ) -> None:
        self._queue = task_queue
        self._channels = channels
        self._store = store
        self._model = orchestrator_model
        self._deps = deps
        self._budget = orchestrator_token_budget
        self._checkpointer = checkpointer
        self._prefs_injector = prefs_injector
        self._skill_store = skill_store
        self._registry = task_registry
        self._model_router = model_router
        self._admission = admission
        self._launch_index = launch_index

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._run_task(task)
            except Exception:
                # Registry already marked failed inside _run_task.
                logger.exception("orchestrator task failed: %s", task.event_id)

    async def _register_task(self, task: OrchestratorTask) -> str:
        """Resolve the TaskRegistry join key for this run.

        User-submitted tasks arrive with ``task.task_id`` already minted and
        a ``queued`` row in place; organic (event-born) tasks get a fresh id
        and row here so every orchestrator run is visible to ``GET /tasks``.
        Returns "" when no registry is wired (tests, headless minimal boots).
        """
        if self._registry is None:
            return task.task_id
        if task.task_id:
            return task.task_id
        task_id = self._registry.mint_task_id()
        await self._registry.create(
            task_id=task_id, origin=task.origin or f"event:{task.topic}",
            instruction=task.instruction,
        )
        return task_id

    async def _run_task(self, task: OrchestratorTask) -> None:
        task_id = await self._register_task(task)
        if self._registry is not None and task_id and self._registry.cancel_requested(task_id):
            # Cancelled while still queued — never start the graph.
            await self._cancel_before_start(task_id)
            return

        # Thread selection:
        #  - subagent_completed wake-up → append a NEW turn to the conversation
        #    that launched the bg task (resume=False so the message is delivered,
        #    but the checkpointer still loads that thread's prior context).
        #  - durable resume (config hot-reload) → continue the persisted thread
        #    from its last checkpoint (resume=True).
        #  - normal → a fresh thread.
        if task.kind == "subagent_completed" and task.parent_thread_id:
            thread_id = task.parent_thread_id
            resume = False
        else:
            resume = bool(task.resume_thread_id)
            thread_id = task.resume_thread_id or _mint_thread_id("orch")
        # Resource governor (Phase 5): heavy tasks (complexity ≥ threshold or
        # a configured heavy subagent_hint) wait inside the slot for a free
        # heavy-task semaphore + an unloaded system before anything starts;
        # a critically full disk raises DiskCriticalError, which the failure
        # path below turns into a failed task with a clear error. No
        # controller wired → null context, pre-Phase-5 behaviour.
        slot = (
            self._admission.slot(task, task_id=task_id)
            if self._admission is not None else contextlib.nullcontext()
        )
        mapped_origin: str | None = None
        try:
            async with slot:
                if (
                    self._registry is not None and task_id
                    and self._registry.cancel_requested(task_id)
                ):
                    # A long admission deferral is plenty of time for the
                    # user to cancel — honor it before the graph starts.
                    await self._cancel_before_start(task_id)
                    return
                # Complexity-based model routing (Phase 4): fresh graph per
                # task makes per-task selection free. Router absent or flag
                # off → the role models the daemon booted with, as before.
                model, deps = self._select_models(task)
                if self._registry is not None and task_id:
                    await self._registry.mark_running(
                        task_id, thread_id=thread_id,
                        complexity=task.complexity,
                        model=model_name_of(model) or None,
                    )
                # Origin-aware ask routing (Phase 3): when the task came in
                # through a channel plugin (origin == a registered channel
                # name, e.g. "telegram"), map this run's thread_id to that
                # channel so Tier-2 asks prefer the submitting surface.
                mapped_origin = await self._map_session_origin(task_id, thread_id)
                await self._execute(
                    task, task_id=task_id, thread_id=thread_id,
                    model=model, deps=deps, resume=resume,
                    origin=mapped_origin or task.origin or None,
                )
        except Exception as exc:
            if self._registry is not None and task_id:
                try:
                    await self._registry.mark_failed(task_id, error=str(exc))
                except Exception:
                    logger.exception("task registry: mark_failed failed")
            raise
        finally:
            if mapped_origin and self._channels.session_origin is not None:
                self._channels.session_origin.clear(thread_id)

    async def _cancel_before_start(self, task_id: str) -> None:
        await self._registry.mark_cancelled(task_id, note="cancelled before start")
        await self._channels.post_event(ChannelEvent(
            payload=TimelinePayload(
                line=f"task {task_id}: cancelled before start",
                cls="event-decision-skipped",
            ),
            task_id=task_id,
        ))

    def _record_async_launch(
        self, ev: StreamEvent, *, thread_id: str, origin: str | None
    ) -> None:
        """Record ``start_async_task`` launches so the watcher can wake us later.

        The orchestrator already streams the tool result (which carries the new
        ``task_id``); we sniff it out and link it to this thread + origin in the
        shared ``LaunchIndex``. No-op when async subagents are disabled.
        """
        if self._launch_index is None or ev.kind != "tool_result":
            return
        if ev.data.get("name") != "start_async_task":
            return
        text = ev.data.get("full") or ev.data.get("preview") or ""
        tid = parse_async_task_id(text)
        if tid:
            self._launch_index.record(tid, thread_id, origin)

    def _select_models(self, task: OrchestratorTask):
        """Resolve (master model, deps) for one task via the ModelRouter.

        With no router (or the routing flag off) this returns the booted
        role models untouched. When the router picks a different subagent
        model, ``deps`` is a per-task copy so the selection can't leak into
        concurrent or later runs.
        """
        if self._model_router is None:
            return self._model, self._deps
        model = self._model_router.model_for(task.complexity, fallback=self._model)
        sub = self._model_router.model_for(
            task.complexity, fallback=self._deps.subagent_model
        )
        deps = self._deps
        if sub is not self._deps.subagent_model and dataclasses.is_dataclass(self._deps):
            deps = dataclasses.replace(self._deps, subagent_model=sub)
        return model, deps

    async def _map_session_origin(self, task_id: str, thread_id: str) -> str | None:
        """Map ``thread_id`` → origin channel for HITL routing; returns the
        channel name when a mapping was set (caller clears it after the run)."""
        session_origin = self._channels.session_origin
        if session_origin is None or self._registry is None or not task_id:
            return None
        try:
            rec = await self._registry.get(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("task registry: get(%s) failed", task_id)
            return None
        origin = rec.origin if rec is not None else ""
        if not origin or self._channels.find(origin) is None:
            return None
        session_origin.set(thread_id, origin)
        return origin

    async def _execute(
        self,
        task: OrchestratorTask,
        *,
        task_id: str,
        thread_id: str,
        model: BaseChatModel | None = None,
        deps: OrchestratorDeps | None = None,
        resume: bool = False,
        origin: str | None = None,
    ) -> None:
        model = model if model is not None else self._model
        deps = deps if deps is not None else self._deps
        # Build-time snapshot keeps only the NON-similarity blocks (prefs +
        # the um_* standing-awareness index). Memory/skill/conversation recall
        # is per-turn now — RetrievalInjectionMiddleware inside
        # build_orchestrator matches them to the task message itself.
        prefs_block = await self._prefs_injector.build_block() if self._prefs_injector else ""
        # Per-agent user-behavior memory: the MEMORY.md index of what this
        # orchestrator has learned about the user (um_note). Unconditional
        # (not similarity-gated) — the whole point is standing awareness.
        from yuyutsava.memory.agent_memory import AgentMemoryStore
        agent_mem_block = await asyncio.to_thread(
            AgentMemoryStore("orchestrator").read_index_block
        )
        blocks = "\n\n".join(
            b for b in (prefs_block, agent_mem_block) if b
        )
        graph = build_orchestrator(
            model=model, deps=deps, budget_tokens=self._budget,
            skill_registry=deps.skill_registry,
            skill_store=self._skill_store,
            checkpointer=self._checkpointer,
            prefs_block=blocks,
            usage_context=UsageContext(task_id=task_id, thread_id=thread_id),
        )
        message = task.render_to_message()

        # Inject in-flight background tasks at the start of every turn so the
        # master is aware of bg work across fresh ``thread_id``s and across
        # context compactions. Empty when no async subagents are configured
        # or no tasks are currently running.
        mirror = getattr(deps, "async_task_mirror", None)
        if mirror is not None:
            block = mirror.render_block()
            if block:
                message = f"{block}\n\n{message}" if isinstance(message, str) else message

        await self._channels.post_event(ChannelEvent(
            payload=LogPayload(text=f"[orch] task {task.event_id[:8]}…\n"),
            task_id=task_id or None, session_id=thread_id,
        ))

        # Route subagent interrupts (tr_* permission prompts, tr_ask_user, and
        # any background AsyncTaskHealthWatcher asks) through the daemon's
        # channel router so the user sees them in the Electron renderer.
        ask_handler = make_ask_handler(
            self._channels,
            default_session_id=thread_id,
            default_agent_path="orchestrator",
        )

        final_text = ""
        cancelled = False
        async for ev in astream_agent_iter(
            graph, message, thread_id=thread_id, recursion_limit=40,
            ask_handler=ask_handler, run_name="orchestrator", resume=resume,
        ):
            await _broadcast(self._channels, ev, task_id=task_id or None, session_id=thread_id)
            # Link any background task this turn launched back to THIS thread +
            # origin, so the watcher can wake us on the right conversation when
            # the task finishes. (deepagents' start_async_task records no parent.)
            self._record_async_launch(ev, thread_id=thread_id, origin=origin)
            if ev.kind == "final":
                final_text = ev.data.get("text", "") or ""
            # Coarse v1 cancellation: honored between stream events. An
            # in-flight LLM/tool call always finishes; the run stops at the
            # next event boundary.
            if (
                self._registry is not None and task_id
                and self._registry.cancel_requested(task_id)
            ):
                cancelled = True
                break

        if cancelled:
            await self._registry.mark_cancelled(task_id, note="cancelled by user")
            if task.proposal_id:
                await self._store.put_decision(
                    proposal_id=task.proposal_id, event_id=task.event_id,
                    outcome="orchestrator_cancelled",
                    action_summary="cancelled by user",
                )
            await self._channels.post_event(ChannelEvent(
                payload=TimelinePayload(
                    line=f"task {task_id}: cancelled by user",
                    cls="event-decision-skipped",
                ),
                task_id=task_id or None, session_id=thread_id,
            ))
            return

        # Decisions exist only for proposal-born tasks. Wake-ups
        # (subagent_completed), direct submissions, and resumes carry an empty
        # proposal_id — recording one would violate decisions_proposal_fk on
        # Postgres and abort the run AFTER the turn already streamed.
        if task.proposal_id:
            await self._store.put_decision(
                proposal_id=task.proposal_id, event_id=task.event_id,
                outcome="orchestrator_done",
                action_summary=(final_text or "(empty)")[:300],
            )
        if self._registry is not None and task_id:
            await self._registry.mark_done(task_id, result_summary=final_text)
        memory = getattr(deps, "memory_store", None)
        if memory is not None and final_text:
            try:
                await memory.add(
                    kind="task_outcome",
                    text=f"{task.instruction[:300]} → {final_text[:700]}",
                    source_thread_id=thread_id,
                    metadata={"topic": task.topic, "event_id": task.event_id},
                )
            except Exception:
                logger.exception("memory: task_outcome write failed")
        await self._channels.post_event(ChannelEvent(
            payload=TimelinePayload(
                line=f"orchestrator: {final_text[:120]}",
                cls="event-action",
            ),
            task_id=task_id or None, session_id=thread_id,
        ))


async def resume_interrupted_tasks(
    registry: object | None,
    task_queue: asyncio.Queue[OrchestratorTask],
    *,
    limit: int = 200,
) -> int:
    """Re-enqueue tasks a previous daemon instance left unfinished.

    Called once at startup. The orchestrator queue lives only in memory, so a
    restart (e.g. a config hot-reload) drops every ``queued`` task and orphans
    the one ``running`` task — its row stays ``running`` because the loop's
    asyncio cancellation raises ``CancelledError`` (not caught as a failure).
    The daemon singleton lock guarantees no *other* process owns these rows,
    so any non-terminal task at boot is ours to resume.

    Each row is re-pushed as an :class:`OrchestratorTask`:

    - ``running`` rows carry their persisted ``thread_id`` + ``resume`` flag so
      the orchestrator continues them from their last LangGraph checkpoint.
    - ``queued`` rows never started, so they re-run fresh on a new thread.

    Returns the number of tasks re-enqueued.
    """
    if registry is None:
        return 0
    rows: list = []
    for status in ("running", "queued"):
        try:
            page, _cursor = await registry.list(status=status, limit=limit)
        except Exception:
            logger.exception("resume: listing %s tasks failed", status)
            continue
        rows.extend(page)

    count = 0
    for rec in rows:
        resume_tid = rec.thread_id or None
        if rec.status == "running" and not resume_tid:
            logger.info(
                "resume: task %s was running but has no thread_id; re-running fresh",
                rec.task_id,
            )
        await task_queue.put(OrchestratorTask(
            proposal_id="",
            event_id="",
            topic=rec.origin or "resume",
            summary=(rec.instruction or "")[:120],
            instruction=rec.instruction or "",
            subagent_hint="general-purpose",
            urgency=2,
            task_id=rec.task_id,
            complexity=rec.complexity if rec.complexity is not None else 3,
            resume_thread_id=resume_tid,
        ))
        count += 1
    if count:
        logger.info("resume: re-enqueued %d interrupted task(s) from previous run", count)
    return count


async def _broadcast(
    channels: ChannelRouter,
    ev: StreamEvent,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
) -> None:
    payload = None
    if ev.kind == "token":
        payload = TokenPayload(text=ev.data.get("text", ""))
    elif ev.kind == "tool_call":
        payload = ToolCallPayload(
            name=ev.data.get("name", "?"),
            args=ev.data.get("args", {}),
        )
    elif ev.kind == "tool_result":
        payload = ToolResultPayload(
            name=ev.data.get("name", "?"),
            preview=ev.data.get("preview", ""),
        )
    elif ev.kind == "log":
        payload = LogPayload(text=ev.data.get("text", ""))
    if payload is not None:
        await channels.post_event(ChannelEvent(
            payload=payload, task_id=task_id, session_id=session_id,
        ))
