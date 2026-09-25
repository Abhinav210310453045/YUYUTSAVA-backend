"""Bind every TaskRunner tool family to one gateway."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools.ask import make_ask_tools
from yuyutsava.agents.task_runner.tools.files import make_files_tools
from yuyutsava.agents.task_runner.tools.host import make_host_tools
from yuyutsava.agents.task_runner.tools.search import make_search_tools
from yuyutsava.agents.task_runner.tools.shell import make_shell_tools
from yuyutsava.agents.task_runner.tools.support import ToolContext
from yuyutsava.agents.task_runner.tools.web import make_web_tools


def bind_tools(
    agent: TaskRunnerAgent,
    *,
    agent_name: str = "agent",
    exec_backend: Any | None = None,
) -> list[BaseTool]:
    """Return the TaskRunner tools bound to *agent*, the gateway they go through.

    Every call is decided by that agent's zones, policy and consent, so a
    stack that shares one agent between its master and subagents shares those
    decisions too — and one that builds its own gets its own.

    ``agent_name`` flows into ``OperationRequest.requesting_agent`` and is used
    by the HITL machinery to append ``/<agent_name>`` to ``agent_path`` in every
    interrupt payload — so the UI knows which subagent is asking.

    ``exec_backend`` (an ``agents.task_runner.exec_backend.ExecBackend``) decides
    WHERE sandbox commands and scripts run. It is installed on *agent*, so
    everything bound to the same agent runs its commands in the same place (the
    Docker bundle's container, for instance).
    """
    if exec_backend is not None:
        agent.exec_backend = exec_backend
    ctx = ToolContext(agent=agent, workspace_root=agent.workspace_root, agent_name=agent_name)
    return [
        *make_files_tools(ctx),
        *make_search_tools(ctx),
        *make_shell_tools(ctx),
        *make_ask_tools(ctx),
        *make_web_tools(ctx),
        *make_host_tools(ctx),
    ]
