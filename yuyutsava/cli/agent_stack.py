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
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.core.llm import chat_model
from yuyutsava.skills.registry import SkillRegistry

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
    async_mirror = None

    if _async_enabled():
        # Local imports keep langgraph_api off the import path when async is off.
        from yuyutsava.async_subagents.host import AsyncSubagentHost
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror

        model = chat_model(settings)
        async_subagents = [general_purpose]
        async_host = AsyncSubagentHost.from_subagents(
            async_subagents,
            model=model,
            checkpointer=checkpointer,
        )
        await asyncio.to_thread(async_host.start)
        async_host_url = async_host.url
        async_mirror = AsyncTaskMirror()
        logger.info(
            "CLI Mode 1 async enabled: host=%s graphs=%s",
            async_host_url, async_host.graph_ids,
        )

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
    )
    return bundle
