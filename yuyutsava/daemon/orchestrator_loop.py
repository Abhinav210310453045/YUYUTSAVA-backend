"""
Orchestrator outer loop.

Pops ``OrchestratorTask``s off the queue and runs the orchestrator graph
on each. Each task gets a fresh thread_id; the graph + checkpoint are
discarded between tasks. This is the rule that bounds context cost
regardless of daemon uptime.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from yuyutsava.agents.orchestrator.agent import OrchestratorDeps, build_orchestrator
from yuyutsava.core.engine import StreamEvent, astream_agent_iter
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.events.store import Store
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("yuyutsava.daemon.orchestrator_loop")


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
    ) -> None:
        self._queue = task_queue
        self._channels = channels
        self._store = store
        self._model = orchestrator_model
        self._deps = deps
        self._budget = orchestrator_token_budget

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._run_task(task)
            except Exception:
                logger.exception("orchestrator task failed: %s", task.event_id)

    async def _run_task(self, task: OrchestratorTask) -> None:
        thread_id = f"orch-{uuid.uuid4()}"
        graph = build_orchestrator(
            model=self._model, deps=self._deps, budget_tokens=self._budget,
        )
        message = task.render_to_message()

        await self._channels.post_event(ChannelEvent(
            kind="log", data={"text": f"[orch] task {task.event_id[:8]}…\n"},
        ))

        final_text = ""
        async for ev in astream_agent_iter(
            graph, message, thread_id=thread_id, recursion_limit=40,
            ask_handler=None,  # orchestrator has no interrupts (no tr_* tools)
        ):
            await _broadcast(self._channels, ev)
            if ev.kind == "final":
                final_text = ev.data.get("text", "") or ""

        await self._store.put_decision(
            proposal_id=task.proposal_id, event_id=task.event_id,
            outcome="orchestrator_done",
            action_summary=(final_text or "(empty)")[:300],
        )
        await self._channels.post_event(ChannelEvent(
            kind="timeline",
            data={"line": f"orchestrator: {final_text[:120]}", "cls": "event-action"},
        ))


async def _broadcast(channels: ChannelRouter, ev: StreamEvent) -> None:
    if ev.kind == "token":
        await channels.post_event(ChannelEvent(kind="token", data={"text": ev.data.get("text", "")}))
    elif ev.kind == "tool_call":
        await channels.post_event(ChannelEvent(kind="tool_call",
                                               data={"name": ev.data.get("name", "?"),
                                                     "args": ev.data.get("args", {})}))
    elif ev.kind == "tool_result":
        await channels.post_event(ChannelEvent(kind="tool_result",
                                               data={"name": ev.data.get("name", "?"),
                                                     "preview": ev.data.get("preview", "")}))
    elif ev.kind == "log":
        await channels.post_event(ChannelEvent(kind="log", data={"text": ev.data.get("text", "")}))
