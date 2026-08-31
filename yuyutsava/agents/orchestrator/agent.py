"""
Orchestrator agent: definitions only. The actual ``build_orchestrator``
factory lives in :mod:`yuyutsava.core.engine` so all master agents (CLI
deepagent + daemon orchestrator) are constructed from the shared engine.

This module keeps the orchestrator-specific *definitions* — ``OrchestratorDeps``
(dep-injection bag) and ``_make_ask_user_tool`` (the orchestrator's master
ask tool wired to the daemon ChannelRouter).
"""

from __future__ import annotations

from yuyutsava.ports.policy import ContextTuning, RemoteSubagentSpec, TaskMirror

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.daemon.channels import AskPrompt, ChannelRouter
from yuyutsava.ports import (
    ArtifactStore,
    CapEnforcer,
    ConversationIndex,
    MemoryStore,
    RuntimeToggles,
    SummaryStore,
    TranscriptStore,
    UsageStore,
)
from yuyutsava.storage.events import Store
from yuyutsava.core.config import SearchConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry

logger = logging.getLogger("yuyutsava.agents.orchestrator")


@dataclass
class OrchestratorDeps:
    """Bag of dependencies the orchestrator and its tools need at call time.

    The ``async_*`` fields stay duck-typed (``object | None``) so this module
    does not pull ``async_subagents`` — and transitively ``langgraph_api`` —
    when background subagents are off. That one is a genuine optional-dependency
    concern, not a cycle workaround.

    The store fields, by contrast, are now typed against
    :mod:`yuyutsava.ports`. They used to be ``object | None`` with the real type
    in a comment because ``agents`` and ``context``/``memory`` import each
    other; ``ports`` is a leaf both sides can depend on, so the types are real
    again and a wrong store no longer type-checks (finding ``F-S05``).

    - ``async_subagents``: ``list[BaseSubAgent]`` whose graphs are hosted by
      ``async_host``. Optional.
    - ``async_host_url``: base URL of the local ``AsyncSubagentHost`` (required
      when ``async_subagents`` is non-empty).
    - ``remote_async_subagents``: ``list[RemoteAsyncSubagentSpec]`` peers hosted
      elsewhere. Optional.
    - ``async_task_mirror``: ``AsyncTaskMirror`` instance used by
      ``BackgroundTaskCapPolicy`` and the turn-start status injector.
    - ``async_max_concurrent``: cap honoured by the cap middleware.
    """

    subagents: dict[str, BaseSubAgent]
    subagent_model: BaseChatModel
    channels: ChannelRouter
    store: Store
    subagent_token_budget: int
    skill_registry: SkillRegistry | None = None
    workspace_root: Path | None = None
    mcp_manager: MCPClientManager | None = None
    search_config: SearchConfig | None = None
    cap_enforcer: CapEnforcer | None = None

    # Async (background) subagent wiring; all optional.
    async_subagents: list[BaseSubAgent] | None = None
    async_host_url: str | None = None
    remote_async_subagents: list[RemoteSubagentSpec] | None = None
    async_task_mirror: TaskMirror | None = None
    async_max_concurrent: int = 8

    # Context controller wiring (yuyutsava.context / yuyutsava.memory); all
    # optional — None disables the corresponding layer. Duck-typed so this
    # module doesn't import the context stack at definition time.
    #
    # - ``artifact_store``: context.artifacts.ArtifactStore — enables tool-
    #   result offloading + the ctx_* retrieval tools.
    # - ``summary_store``: context.summary_store.ThreadSummaryStore —
    #   compaction summaries persist here.
    # - ``memory_store``: memory.store.MemoryStore — mem_* tools + summary
    #   embedding + task-outcome writes.
    # - ``context_settings``: context.config.ContextSettings — enables the
    #   compaction middleware (and sizes the offload threshold).
    # - ``compaction_model``: cheap chat model for summarization; falls back
    #   to the agent's own model when None.
    artifact_store: ArtifactStore | None = None
    summary_store: SummaryStore | None = None
    memory_store: MemoryStore | None = None
    transcript_store: TranscriptStore | None = None
    # context.transcript_index.PgTranscriptIndex — when set, the master gets
    # a per-turn ConversationInjector (recall of its own swept turns).
    transcript_index: ConversationIndex | None = None
    context_settings: ContextTuning | None = None
    compaction_model: BaseChatModel | None = None

    # Phase 4 cost tracking: daemon.usage.UsageStore — when set, every
    # master/subagent model call writes one llm_usage row (UsagePolicy).
    usage_store: UsageStore | None = None

    # prefs.runtime.RuntimeSettings — the user's dedicated-subagent switches.
    # Read at build time (a fresh orchestrator graph per task means a disabled
    # subagent simply never enters the roster) and enforced per call by
    # SubagentGatePolicy. None → every registered subagent is available.
    runtime_settings: RuntimeToggles | None = None


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
        from yuyutsava.core.agent_context import current_context

        ctx = current_context()
        ask = AskPrompt(
            ask_id=str(uuid.uuid4()),
            title="Orchestrator question",
            body=question,
            options=list(options) if options else [],
            interrupt_value={"type": "orchestrator_ask", "question": question, **ctx},
            session_id=ctx.get("session_id"),
            agent_path=ctx.get("agent_path"),
            # The orchestrator runs behind the user's back — its questions
            # belong in the Inbox, not inside whatever chat is open.
            surface="background",
            thread_id=ctx.get("session_id"),
            task_id=ctx.get("task_id"),
            agent_label="Orchestrator",
        )
        return await channels.post_ask(ask)

    return ask_user
