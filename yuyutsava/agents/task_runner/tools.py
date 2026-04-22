"""
LangChain @tool wrappers that expose TaskRunnerAgent as callable tools.

Use ``bind_tools(workspace_root)`` to get the four tools bound to a specific
workspace. The factory is the only public API here — callers should never
import the tool functions directly, because they are closures that need a
workspace_root to function correctly.

The module keeps a registry so the same TaskRunnerAgent instance is reused
for each workspace_root path, avoiding redundant instantiation.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.types import OperationRequest, OperationType

# ---------------------------------------------------------------------------
# TaskRunnerAgent registry — one instance per resolved workspace root
# ---------------------------------------------------------------------------

_registry: dict[str, TaskRunnerAgent] = {}


def _get_or_create_agent(workspace_root: Path) -> TaskRunnerAgent:
    key = str(workspace_root.resolve())
    if key not in _registry:
        _registry[key] = TaskRunnerAgent(workspace_root)
    return _registry[key]


# ---------------------------------------------------------------------------
# Tool factory — creates the 4 tools bound to workspace_root
# ---------------------------------------------------------------------------


def bind_tools(workspace_root: Path) -> list[BaseTool]:
    """
    Return the four TaskRunner tools bound to *workspace_root*.

    The tools are:
      - tr_read_file       — read any file (zone-checked)
      - tr_write_file      — write/create a file (zone-checked)
      - tr_delete_file     — delete a file or directory (zone-checked)
      - tr_execute_in_sandbox — run a shell command inside the sandbox zone

    Each tool returns a JSON string (OperationResponse.model_dump_json()) so
    the calling LLM sees a structured, parseable result in its ToolMessage.
    """
    agent = _get_or_create_agent(workspace_root)

    # ------------------------------------------------------------------ #
    # tr_read_file                                                         #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_read_file(path: str, reason: str) -> str:
        """Read a file via the TaskRunner gateway (zone-checked).

        Args:
            path: Absolute path to the file.
            reason: Why you need this file (shown to user if permission required).

        Returns:
            JSON: {status, result (file content), error, alternatives}
        """
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.READ,
            paths=[path],
            reason=reason,
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_write_file                                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_write_file(path: str, content: str, reason: str) -> str:
        """Write/create a file via the TaskRunner gateway (zone-checked). Creates parent dirs; overwrites existing files.

        Args:
            path: Absolute path to write.
            content: Text content to write.
            reason: Why you are writing this file (shown to user if permission required).

        Returns:
            JSON: {status, result ({"written_to": path}), error}
        """
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.WRITE,
            paths=[path],
            reason=reason,
            additional_context={"content": content},
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_delete_file                                                       #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_delete_file(path: str, reason: str) -> str:
        """Delete a file or directory via the TaskRunner gateway (zone-checked).

        Args:
            path: Absolute path to delete.
            reason: Why you are deleting this (shown to user if permission required).

        Returns:
            JSON: {status, result ({"deleted": path}), error}
        """
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.DELETE,
            paths=[path],
            reason=reason,
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_execute_in_sandbox                                                #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_execute_in_sandbox(
        command: str,
        reason: str,
        timeout: int = 120,
    ) -> str:
        """Execute a shell command in the sandbox directory (always auto-allowed, cwd=_sandbox/).

        Args:
            command: Shell command to run (e.g. "python3 _sandbox/analyze.py").
            reason: Why you are running this command.
            timeout: Max execution time in seconds (default 120).

        Returns:
            JSON: {status, result ({"stdout", "stderr", "exit_code"}), error}
        """
        sandbox_path = str(agent.sandbox_root)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": sandbox_path,
            },
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    return [tr_read_file, tr_write_file, tr_delete_file, tr_execute_in_sandbox]
