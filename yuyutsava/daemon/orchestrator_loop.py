"""
Orchestrator outer loop.

Pops ``OrchestratorTask``s off the queue and runs the orchestrator graph
on each. Each task gets a fresh thread_id; the graph + checkpoint are
discarded between tasks. This is the rule that bounds context cost
regardless of daemon uptime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from yuyutsava.agents.orchestrator.agent import OrchestratorDeps, build_orchestrator
from yuyutsava.core.engine import StreamEvent, astream_agent_iter
from yuyutsava.daemon.channels import AskPrompt, ChannelEvent, ChannelRouter
from yuyutsava.daemon.checkpointing import thread_id as _mint_thread_id
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.events.store import Store
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger("yuyutsava.daemon.orchestrator_loop")


# ---------------------------------------------------------------------------
# Interrupt formatting helpers (used by ask_handler inside _run_task)
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
    return iv.get("command") or iv.get("reason") or json.dumps(iv)[:300]


def _options_for_interrupt(iv: dict) -> list[str]:
    if not isinstance(iv, dict):
        return ["approve", "reject"]
    t = iv.get("type", "")
    if t == "user_question":
        return list(iv.get("options") or [])
    return ["approve", "reject"]


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
    ) -> None:
        self._queue = task_queue
        self._channels = channels
        self._store = store
        self._model = orchestrator_model
        self._deps = deps
        self._budget = orchestrator_token_budget
        self._checkpointer = checkpointer
        self._prefs_injector = prefs_injector

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
        thread_id = _mint_thread_id("orch")
        prefs_block = self._prefs_injector.build_block() if self._prefs_injector else ""
        graph = build_orchestrator(
            model=self._model, deps=self._deps, budget_tokens=self._budget,
            skill_registry=self._deps.skill_registry,
            checkpointer=self._checkpointer,
            prefs_block=prefs_block,
        )
        message = task.render_to_message()

        await self._channels.post_event(ChannelEvent(
            kind="log", data={"text": f"[orch] task {task.event_id[:8]}…\n"},
        ))

        # Route subagent interrupts (tr_* permission prompts, tr_ask_user) through
        # the daemon's channel router so the user sees them in the web UI / terminal.
        async def ask_handler(interrupt_value: dict) -> str:
            iv = interrupt_value if isinstance(interrupt_value, dict) else {}
            ask = AskPrompt(
                ask_id=str(uuid.uuid4()),
                title=_title_for_interrupt(interrupt_value),
                body=_body_for_interrupt(interrupt_value),
                options=_options_for_interrupt(interrupt_value),
                interrupt_value=dict(iv),
                session_id=iv.get("session_id") or thread_id,
                agent_path=iv.get("agent_path") or "orchestrator",
            )
            return await self._channels.post_ask(ask)

        final_text = ""
        async for ev in astream_agent_iter(
            graph, message, thread_id=thread_id, recursion_limit=40,
            ask_handler=ask_handler, run_name="orchestrator",
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
