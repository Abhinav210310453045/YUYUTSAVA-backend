"""Factory for the CLI's agent stack.

A single function builds everything the chat REPL needs: skill registry,
task runner, general-purpose subagent, and the compiled deepagent bundle.

Pulled out of cli.py so tests, scripts, and any future second entry-point
can share one construction path instead of copy-pasting 30 lines of wiring.

Async (background) subagents are env-gated for v1: set ``YUYUTSAVA_ASYNC_SUBAGENTS=1``
to wire up an in-process ``AsyncSubagentHost`` + watcher + ``CliHitlBridge``.
``AgentBundle.async_host`` / ``async_task_mirror`` carry the live objects so
the REPL can drain bridge events and tear down on exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.context.artifacts import SqliteArtifactStore
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.summary_store import SqliteThreadSummaryStore
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig, _env
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.core.llm import chat_model
from yuyutsava.core.config import llm_settings_from_env
from yuyutsava.memory.config import MemorySettings
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.cli.agent_stack")


def _async_enabled() -> bool:
    return os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes")


async def build_cli_agent_stack(
    workspace: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int,
    execution_mode: Literal["local", "docker"],
    docker_settings: DockerSettings,
    local_settings: LocalSettings,
    permission_check: bool,
    search_config: SearchConfig,
    checkpointer: BaseCheckpointSaver,
) -> AgentBundle:
    """Build the CLI deepagent + its subagent stack.

    The current sync subagent list is just ``GeneralPurposeAgent`` — passing
    it causes deepagents to name-match-override its built-in default with our
    tighter prompt + lazy tool discovery via ToolRegistry.

    When ``YUYUTSAVA_ASYNC_SUBAGENTS=1``: also stand up an in-process
    ``AsyncSubagentHost`` hosting the same subagent(s) under ``-bg`` names,
    plus an ``AsyncTaskMirror``, an ``AsyncTaskHealthWatcher``, and a
    ``CliHitlBridge`` for routing interrupts to stdin.
    """
    # Context controller: CLI chat threads are the longest-lived in the
    # system, so offload + compaction are always on. Stores are the SQLite
    # twins in state.db regardless of YUYUTSAVA_STORAGE_BACKEND — the CLI
    # has no pool lifecycle owner yet (the daemon does); checkpoints still
    # honor the postgres backend via build_checkpointer.
    artifact_store = SqliteArtifactStore(state_db_path())
    summary_store = SqliteThreadSummaryStore(state_db_path())
    context_settings = ContextSettings.from_env(
        "cli", provider=_env("LLM_PROVIDER", None, "groq"),
    )
    compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)
    memory_store = None
    if MemorySettings.from_env().enabled:
        from yuyutsava.memory.store import SqliteMemoryStore
        memory_store = SqliteMemoryStore(state_db_path())

    skill_registry = SkillRegistry(workspace_dir=workspace)
    sandbox_root_for_tr = (
        local_settings.sandbox_dir.resolve()
        if local_settings.sandbox_dir is not None
        else (workspace / "_sandbox").resolve()
    )
    task_runner = TaskRunnerAgent(
        workspace_root=workspace,
        sandbox_root=sandbox_root_for_tr,
    )
    general_purpose = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        search_config=search_config,
    )

    async_subagents = None
    async_host_url = None
    async_host = None
    async_host_attachment = None
    async_mirror = None

    if _async_enabled():
        # Local imports keep langgraph_api off the import path when async is off.
        from yuyutsava.async_subagents.host import AsyncSubagentHost
        from yuyutsava.async_subagents.host_lock import acquire_or_attach_host
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror

        model = chat_model(settings)
        async_subagents = [general_purpose]

        # First-come-wins shared host. If a daemon (or another chat) is
        # already running and owns the LangGraph dev server, attach to its
        # URL instead of starting a second one.
        def _build_host() -> AsyncSubagentHost:
            return AsyncSubagentHost.from_subagents(
                async_subagents,
                model=model,
                checkpointer=checkpointer,
            )

        attachment = await asyncio.to_thread(
            acquire_or_attach_host, factory=_build_host
        )
        async_host_attachment = attachment
        async_host_url = attachment.url
        async_host = attachment.host  # None when attached to another owner
        async_mirror = AsyncTaskMirror()
        if attachment.host is not None:
            logger.info(
                "CLI async host: owner @ %s graphs=%s",
                async_host_url, attachment.host.graph_ids,
            )
        else:
            logger.info("CLI async host: attached to running owner @ %s", async_host_url)

    bundle = build_cli_deepagent(
        workspace,
        settings,
        bash_timeout_sec=bash_timeout_sec,
        execution_mode=execution_mode,
        docker_settings=docker_settings,
        local_settings=local_settings,
        permission_check=permission_check,
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
        context_settings=context_settings,
        compaction_model=compaction_model,
    )
    return bundle
