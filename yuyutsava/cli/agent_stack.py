"""Factory for the CLI's agent stack.

A single function builds everything the chat REPL needs: skill registry,
task runner, general-purpose subagent, and the compiled deepagent bundle.

Pulled out of cli.py so tests, scripts, and any future second entry-point
can share one construction path instead of copy-pasting 30 lines of wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.skills.registry import SkillRegistry


def build_cli_agent_stack(
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

    The current subagent list is just `GeneralPurposeAgent` — passing it as a
    subagent spec causes deepagents to name-match-override its built-in default
    with our tighter prompt + lazy tool discovery via ToolRegistry.
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
    return build_cli_deepagent(
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
    )
