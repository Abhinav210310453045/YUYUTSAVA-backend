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
from yuyutsava.core.llm import model_name_of
from yuyutsava.daemon.usage import UsageContext
from yuyutsava.storage.ids import mint_thread_id as _mint_thread_id
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.storage.events import Store
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger("yuyutsava.daemon.orchestrator_loop")


# ---------------------------------------------------------------------------
# Interrupt formatting helpers (used by the ask_handler factory)
# ---------------------------------------------------------------------------

def _title_for_interrupt(iv: dict) -> str:
    if not isinstance(iv, dict):
        return "Permission request"
    t = iv.get("type", "")
    if t == "task_runner_permission":
        op = (iv.get("operation") or "").upper()
        return f"Permission: {op}"
    if t == "user_question":
        return "Subagent question"
    return iv.get("title") or "Permission request"


def _body_for_interrupt(iv: dict) -> str:
    if not isinstance(iv, dict):
        return str(iv)
    t = iv.get("type", "")
    if t == "task_runner_permission":
        paths = iv.get("paths", [])
        op = iv.get("operation", "?")
        reason = iv.get("reason", "")
        risk = iv.get("risk_level", "")
        zone = iv.get("zone", "")
        path_str = ", ".join(paths) if isinstance(paths, list) else str(paths)
        return f"{op} {path_str}\nzone: {zone}  risk: {risk}\n\n{reason}"
    if t == "user_question":
        return iv.get("question", "")
    # PermissionMiddleware (raw execute) — show both command and reason so
    # the Electron card carries the same "what / why" that the CLI prompts
    # already include.
    if t == "permission_request":
        command = iv.get("command", "")
        reason = iv.get("reason", "")
        if command and reason:
            return f"{command}\n\n{reason}"
        return command or reason or json.dumps(iv)[:300]
    return iv.get("command") or iv.get("reason") or json.dumps(iv)[:300]


def _options_for_interrupt(iv: dict) -> list[str]:
    if not isinstance(iv, dict):
        return ["approve", "reject"]
    t = iv.get("type", "")
    if t == "user_question":
        return list(iv.get("options") or [])
    return ["approve", "reject"]


def make_ask_handler(
    channels: ChannelRouter,
    *,
    default_session_id: str,
    default_agent_path: str = "orchestrator",
):
    """Factory producing the ask handler the orchestrator + bg watcher share.

    Both the master's streaming loop and the ``AsyncTaskHealthWatcher`` route
    interrupt values into ``ChannelRouter.post_ask`` with the same shape.
    Extracting it here keeps the formatting consistent and lets the watcher
    reuse the daemon's HITL surface without duplicating logic.
    """

    async def ask_handler(interrupt_value: dict) -> str:
        iv = interrupt_value if isinstance(interrupt_value, dict) else {}
        ask = AskPrompt(
            ask_id=str(uuid.uuid4()),
            title=_title_for_interrupt(interrupt_value),
            body=_body_for_interrupt(interrupt_value),
            options=_options_for_interrupt(interrupt_value),
            interrupt_value=dict(iv),
            session_id=iv.get("session_id") or default_session_id,
            agent_path=iv.get("agent_path") or default_agent_path,
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
        memory_injector: object | None = None,  # yuyutsava.context.injector.MemoryInjector
        task_registry: object | None = None,  # yuyutsava.daemon.task_registry.TaskRegistry
        model_router: object | None = None,  # yuyutsava.core.model_router.ModelRouter
        admission: object | None = None,  # yuyutsava.daemon.resources.AdmissionController
    ) -> None:
        self._queue = task_queue
        self._channels = channels
        self._store = store
        self._model = orchestrator_model
        self._deps = deps
        self._budget = orchestrator_token_budget
        self._checkpointer = checkpointer
        self._prefs_injector = prefs_injector
        self._memory_injector = memory_injector
        self._registry = task_registry
        self._model_router = model_router
        self._admission = admission

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
            task_id=task_id, origin=f"event:{task.topic}",
            instruction=task.instruction,
        )
        return task_id

    async def _run_task(self, task: OrchestratorTask) -> None:
        task_id = await self._register_task(task)
        if self._registry is not None and task_id and self._registry.cancel_requested(task_id):
            # Cancelled while still queued — never start the graph.
            await self._cancel_before_start(task_id)
            return

        thread_id = _mint_thread_id("orch")
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
                    model=model, deps=deps,
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
    ) -> None:
        model = model if model is not None else self._model
        deps = deps if deps is not None else self._deps
        prefs_block = self._prefs_injector.build_block() if self._prefs_injector else ""
        # Relevant past context (summaries, outcomes, saved facts) recalled
        # by similarity to the task text — same informational-block contract
        # as prefs. Empty when memory is disabled or nothing matches.
        memory_block = ""
        if self._memory_injector is not None:
            memory_block = await self._memory_injector.build_block(
                f"{task.summary}\n{task.instruction}"
            )
        blocks = "\n\n".join(b for b in (prefs_block, memory_block) if b)
        graph = build_orchestrator(
            model=model, deps=deps, budget_tokens=self._budget,
            skill_registry=deps.skill_registry,
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
            ask_handler=ask_handler, run_name="orchestrator",
        ):
            await _broadcast(self._channels, ev, task_id=task_id or None, session_id=thread_id)
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
