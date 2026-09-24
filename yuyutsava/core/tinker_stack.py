"""Factory for a card-bound TinkerAgent stack.

The tinker sibling of :func:`yuyutsava.core.agent_stack.build_agent_stack`: one
call assembles the retrieval stores, context-controller wiring, subagents, and
the compiled bundle for ONE TODO card. The daemon's
:class:`~yuyutsava.daemon.conversation_manager.ConversationManager` caches the
returned bundle per card (``tinker:<card_id>``) — the conversation thread is
pinned to ``todo:<card_id>`` by the manager, not here.

Board access stays on the exchange: this module never touches the todo store.
It deliberately does NOT call ``set_default_todo_store`` — the daemon's
bootstrap already wired the RoutedStore, and the tinker's ``todo_*`` tools
resolve it lazily through ``get_default_exchange()``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.config.search import SearchConfig
from yuyutsava.core.async_host import (
    Background,
    BackgroundStores,
    async_subagents_enabled,
    start_background,
)
from yuyutsava.core.deps import MasterDeps
from yuyutsava.core.engine import AgentBundle, build_tinker_agent
from yuyutsava.core.stores import build_master_stores
from yuyutsava.llm.settings import LlmSettings
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.todoboard.recall import get_default_note_index

logger = logging.getLogger("yuyutsava.agents.tinker")


async def build_tinker_stack(
    workspace: Path,
    settings: LlmSettings,
    *,
    card_id: str,
    card_workspace: Path,
    bash_timeout_sec: int,
    search_config: SearchConfig,
    checkpointer: BaseCheckpointSaver,
    usage_store: Any | None = None,
    mcp_manager: Any | None = None,
    prefs_store: Any | None = None,
    cap_enforcer: Any | None = None,
    extra_tools: "list[Any] | None" = None,
) -> AgentBundle:
    """Build one card's TinkerAgent bundle.

    ``workspace`` is the daemon/CLI workspace (skill discovery scope);
    ``card_workspace`` is the card's blob dir — the agent's actual WORKSPACE
    zone, where tr_* and vis_* output lands. ``mcp_manager`` is the daemon's
    :class:`~yuyutsava.mcp.loader.MCPClientManager`; when present the tinker
    gets the user-configured MCP tools scoped to ``"tinker"``, same wiring as
    the orchestrator master.
    """
    skill_registry = SkillRegistry(workspace_dir=WorkspaceLayout.for_workspace(workspace).skills)
    # Same wiring as the conversational stack (the retrieval sync also indexes
    # the bundled tinker skills, agent-tagged by their bundled/tinker/ dir).
    # The bundle owns the pool/embedder and closes them in aclose.
    stores = await build_master_stores("tinker", skill_registry)
    memory_store, skill_store = stores.memory_store, stores.skill_store

    # The general-purpose subagent works inside the SAME card workspace, so a
    # delegated research/build step lands its files on the card. Consent rides
    # the process default the daemon bootstrap installed (set_default_consent).
    card_ws = card_workspace.resolve()
    card_layout = WorkspaceLayout.for_workspace(card_ws)
    # Materialize <card>/.yuyutsava (sync mkdir → off-loop; see storage.paths).
    await asyncio.to_thread(card_layout.ensure)
    task_runner = TaskRunnerAgent(
        workspace_root=card_ws,
        sandbox_root=card_layout.sandbox,
    )
    general_purpose = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        can_write_skills=True,
        search_config=search_config,
        memory_store=memory_store,
        skill_store=skill_store,
    )

    background = Background()
    if async_subagents_enabled():
        # Inside the daemon this always attaches to the running owner.
        background = await start_background(
            settings,
            checkpointer,
            [general_purpose],
            BackgroundStores(
                artifact_store=stores.context.artifacts,
                context_settings=stores.context_settings,
                summary_store=stores.context.summaries,
                memory_store=memory_store,
            ),
        )
        logger.info(
            "tinker[%s]: async host %s @ %s",
            card_id,
            "owner" if background.host is not None else "attached",
            background.host_url,
        )

    # User-configured MCP servers (scope "tinker") + board-note recall. Both
    # resolve to None/[] gracefully: no MCP manager outside the daemon, no
    # note index on SQLite-only deployments.
    mcp_tools = mcp_manager.tools_for("tinker") if mcp_manager is not None else []

    note_index = get_default_note_index()

    bundle = build_tinker_agent(
        card_id,
        card_ws,
        settings,
        deps=MasterDeps(
            bash_timeout_sec=bash_timeout_sec,
            permission_check=True,
            search_config=search_config,
            checkpointer=checkpointer,
            subagents=[general_purpose],
            async_subagents=background.subagents,
            async_host_url=background.host_url,
            async_task_mirror=background.mirror,
            async_host=background.host,
            async_host_attachment=background.attachment,
            artifact_store=stores.context.artifacts,
            summary_store=stores.context.summaries,
            memory_store=memory_store,
            transcript_store=stores.context.transcripts,
            context_settings=stores.context_settings,
            compaction_model=stores.compaction_model,
            skill_store=skill_store,
            transcript_index=stores.context.transcript_index,
            usage_store=usage_store,
            mcp_tools=mcp_tools,
            prefs_store=prefs_store,
            cap_enforcer=cap_enforcer,
            extra_tools=extra_tools,
            # budget_tokens deliberately unset — build_tinker_agent supplies
            # TINKER_BUDGET_TOKENS, which is what this call has always got.
        ),
        skill_registry=skill_registry,
        note_index=note_index,
    )
    bundle.pg_pool = stores.pg_pool
    bundle.embedder = stores.embedder
    return bundle
