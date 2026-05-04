"""
Orchestrator agent: small ``create_react_agent`` with three tools.

Critical design choice: NOT a deepagents agent. We replace the heavy
default DeepAgent prompt with our own ≈300-token routing prompt. Each
task gets a fresh thread_id; nothing accumulates across events.

The ``dispatch`` tool is the bridge to specialised subagents. It builds the
target subagent's graph fresh, runs it via ``astream_agent_iter``, pipes
its stream events into the channel router (so the user sees what the
subagent is doing), routes its tool-permission interrupts through the
channel router's ``post_ask``, then returns the subagent's final message
as a short summary to the orchestrator.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.orchestrator.prompts import render_system_prompt
from yuyutsava.core.engine import StreamEvent, astream_agent_iter
from yuyutsava.daemon.budget import BudgetMiddleware
from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, ChannelRouter,
)
from yuyutsava.events.store import Store
from yuyutsava.events.tools import make_recall_tool

logger = logging.getLogger("yuyutsava.agents.orchestrator")


@dataclass
class OrchestratorDeps:
    """Bag of dependencies the dispatch/ask_user tools need at call time."""

    subagents: dict[str, BaseSubAgent]
    subagent_model: BaseChatModel
    channels: ChannelRouter
    store: Store
    subagent_token_budget: int


def build_orchestrator(
    *,
    model: BaseChatModel,
    deps: OrchestratorDeps,
    budget_tokens: int,
) -> CompiledStateGraph:
    """Build a fresh orchestrator graph. Cheap — call once per OrchestratorTask."""
    capabilities = render_capabilities_block(list(deps.subagents.values()))
    system_prompt = render_system_prompt(capabilities)

    tools: list[BaseTool] = [
        _make_dispatch_tool(deps),
        _make_ask_user_tool(deps.channels),
        make_recall_tool(deps.store),
    ]

    budget = BudgetMiddleware(max_input_tokens=budget_tokens, role="orchestrator")
    return create_react_agent(
        model=model,
        tools=tools,
        prompt=system_prompt,
        checkpointer=MemorySaver(),
        middleware=[budget],
    )


# ---------------------------------------------------------------------------
# dispatch tool
# ---------------------------------------------------------------------------


def _make_dispatch_tool(deps: OrchestratorDeps) -> BaseTool:
    @tool
    async def dispatch(subagent: str, instruction: str) -> str:
        """Run a specialised subagent on this event, wait for its summary, return it.

        Pick ``subagent`` from AVAILABLE SUBAGENTS exactly. ``instruction``
        should be the user-approved proposal text plus any minimal context
        the subagent needs (typically just the event_id and intent).
        """
        sa = deps.subagents.get(subagent)
        if sa is None:
            return f"error: unknown subagent {subagent!r}"

        # Fresh per-invocation graph so checkpoints don't leak across tasks.
        graph = sa.build_react_agent(deps.subagent_model, MemorySaver())
        thread_id = f"{sa.name}-{uuid.uuid4()}"

        # Subagent interrupts (tr_* permission asks) flow through the channel.
        async def ask_handler(interrupt_value: dict) -> str:
            ask = AskPrompt(
                ask_id=str(uuid.uuid4()),
                title=_title_for_interrupt(interrupt_value),
                body=_body_for_interrupt(interrupt_value),
                options=_options_for_interrupt(interrupt_value),
                interrupt_value=dict(interrupt_value) if isinstance(interrupt_value, dict) else {},
            )
            return await deps.channels.post_ask(ask)

        await deps.channels.post_event(
            ChannelEvent(kind="timeline",
                         data={"ts": time.time(),
                               "line": f"dispatched: {subagent} ← {instruction[:80]}",
                               "cls": "event-action"})
        )

        final_text = ""
        async for ev in astream_agent_iter(
            graph, instruction, thread_id=thread_id, recursion_limit=80,
            ask_handler=ask_handler,
        ):
            await _broadcast_stream(deps.channels, ev)
            if ev.kind == "final":
                final_text = ev.data.get("text", "") or ""

        summary = (final_text or "(no summary returned)").strip().splitlines()[0]
        # Cap to keep the orchestrator's context small.
        return summary[:500]

    return dispatch


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


async def _broadcast_stream(channels: ChannelRouter, ev: StreamEvent) -> None:
    """Convert an engine StreamEvent into a ChannelEvent and broadcast."""
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


# ---------------------------------------------------------------------------
# ask_user tool
# ---------------------------------------------------------------------------


def _make_ask_user_tool(channels: ChannelRouter) -> BaseTool:
    @tool
    async def ask_user(question: str, options: list[str] | None = None) -> str:
        """Ask the user a clarifying question and wait for the response.

        ``options`` is an optional list of one-word choices; if empty, the user
        responds with free text. Returns the user's response string.
        """
        ask = AskPrompt(
            ask_id=str(uuid.uuid4()),
            title="Orchestrator question",
            body=question,
            options=list(options) if options else [],
            interrupt_value={"type": "orchestrator_ask", "question": question},
        )
        return await channels.post_ask(ask)

    return ask_user
