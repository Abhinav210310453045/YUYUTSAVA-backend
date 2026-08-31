"""Factory for a card-bound TinkerAgent stack.

The tinker sibling of :func:`yuyutsava.cli.agent_stack.build_agent_stack`: one
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

import logging
import os
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.cli.agent_stack import _build_retrieval_stores
from yuyutsava.context.config import ContextSettings
from yuyutsava.core.config import LlmSettings, SearchConfig, _env, llm_settings_from_env
from yuyutsava.core.engine import AgentBundle, build_tinker_agent
from yuyutsava.llm import chat_model
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.agents.tinker")


def _async_enabled() -> bool:
    return os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes")


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
    context_settings = ContextSettings.from_env(
        "tinker", provider=_env("LLM_PROVIDER", None, "groq"),
    )
    compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)

    skill_registry = SkillRegistry(workspace_dir=workspace)

    # Same retrieval wiring as the conversational stack: pgvector stores when
    # Postgres is up (the sync inside also indexes the bundled tinker skills,
    # agent-tagged by their bundled/tinker/ dir), SQLite keyword twins
    # otherwise. The bundle owns the pool/embedder and closes them in aclose.
    memory_store, skill_store, pg_pool, embedder = await _build_retrieval_stores(
        skill_registry
    )

    # One selection, shared with the CLI and the daemon (Phase 3 step 3.5).
    from yuyutsava.storage.backend import StorageSettings as _SS
    from yuyutsava.storage.factory import StoreFactory as _SF

    _ctx = _SF(_SS.from_env(), pg_pool=pg_pool, embedder=embedder).context_stores(
        semantic_recall=context_settings.semantic_recall
    )
    artifact_store = _ctx.artifacts
    summary_store = _ctx.summaries
    transcript_store = _ctx.transcripts
    transcript_index = _ctx.transcript_index

    # The general-purpose subagent works inside the SAME card workspace, so a
    # delegated research/build step lands its files on the card. Consent rides
    # the process default the daemon bootstrap installed (set_default_consent).
    card_ws = card_workspace.resolve()
    task_runner = TaskRunnerAgent(
        workspace_root=card_ws,
        sandbox_root=card_ws / "_sandbox",
    )
    general_purpose = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        can_write_skills=True,
        search_config=search_config,
        memory_store=memory_store,
        skill_store=skill_store,
    )

    async_subagents = None
    async_host_url = None
    async_host = None
    async_host_attachment = None
    async_mirror = None

    if _async_enabled():
        import asyncio

        from yuyutsava.async_subagents.host import (
            AsyncSubagentHost,
            resolve_allow_blocking,
        )
        from yuyutsava.async_subagents.host_lock import acquire_or_attach_host
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror

        model = chat_model(settings)
        async_subagents = [general_purpose]
        allow_blocking = resolve_allow_blocking(default=True)

        # First-come-wins shared host: inside the daemon this always attaches
        # to the already-running owner instead of starting a second server.
        def _build_host() -> AsyncSubagentHost:
            # Background graphs get the same context controllers as the
            # masters (tool-result offload + compaction + ctx_* readback).
            from yuyutsava.context.tools import make_context_tools
            from yuyutsava.core.engine import context_middleware

            # Host-only compaction model: host graphs run on the uvicorn loop,
            # and the bundle's compaction model belongs to this loop — Gemini
            # SDK clients bind to their first loop (see
            # llm/quirks/loop_affinity). ``model`` above is already host-only.
            host_compaction_model = chat_model(
                llm_settings_from_env("compaction"), temperature=0.0
            )

            return AsyncSubagentHost.from_subagents(
                async_subagents,
                model=model,
                checkpointer=checkpointer,
                allow_blocking=allow_blocking,
                middleware_factory=lambda sa: context_middleware(
                    model=model,
                    artifact_store=artifact_store,
                    context_settings=context_settings,
                    summary_store=summary_store,
                    memory_store=memory_store,
                    transcript_store=None,  # bg thread ids are host-minted;
                    # transcripts serve interactive resume — skip them here.
                    compaction_model=host_compaction_model,
                    role=f"{sa.name}-bg",
                ),
                extra_tools_factory=(
                    (lambda: make_context_tools(artifact_store))
                    if artifact_store is not None else None
                ),
            )

        attachment = await asyncio.to_thread(
            acquire_or_attach_host, factory=_build_host
        )
        async_host_attachment = attachment
        async_host_url = attachment.url
        async_host = attachment.host  # None when attached to another owner
        async_mirror = AsyncTaskMirror()
        logger.info(
            "tinker[%s]: async host %s @ %s",
            card_id, "owner" if attachment.host is not None else "attached", async_host_url,
        )

    # User-configured MCP servers (scope "tinker") + board-note recall. Both
    # resolve to None/[] gracefully: no MCP manager outside the daemon, no
    # note index on SQLite-only deployments.
    mcp_tools = mcp_manager.tools_for("tinker") if mcp_manager is not None else []
    from yuyutsava.todoboard.recall import get_default_note_index
    note_index = get_default_note_index()

    bundle = build_tinker_agent(
        card_id,
        card_ws,
        settings,
        bash_timeout_sec=bash_timeout_sec,
        permission_check=True,
        search_config=search_config,
        checkpointer=checkpointer,
        subagents=[general_purpose],
        async_subagents=async_subagents,
        async_host_url=async_host_url,
        async_task_mirror=async_mirror,
        async_host=async_host,
        async_host_attachment=async_host_attachment,
        artifact_store=artifact_store,
        summary_store=summary_store,
        memory_store=memory_store,
        transcript_store=transcript_store,
        context_settings=context_settings,
        compaction_model=compaction_model,
        skill_store=skill_store,
        transcript_index=transcript_index,
        skill_registry=skill_registry,
        usage_store=usage_store,
        mcp_tools=mcp_tools,
        note_index=note_index,
        prefs_store=prefs_store,
        cap_enforcer=cap_enforcer,
        extra_tools=extra_tools,
    )
    bundle.pg_pool = pg_pool
    bundle.embedder = embedder
    return bundle
