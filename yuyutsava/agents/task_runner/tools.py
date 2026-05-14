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

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.models.operations import OperationRequest, OperationType

_log = logging.getLogger("yuyutsava.agents.task_runner.tools")


def _resolve_path(raw: str, workspace_root: Path) -> str:
    """
    Translate a path the model may have taken from ls/glob virtual output.

    ls/glob in LocalShellBackend (virtual_mode=True) returns paths anchored at
    the virtual root '/' — e.g. '/README.md' actually means
    '<workspace_root>/README.md'.  If the raw path is NOT under the workspace
    or sandbox, and its depth from '/' is ≤ 3 levels (a strong signal it is a
    virtual path rather than a real absolute path), prefix it with the workspace
    root and return a corrected path plus a warning so the model can learn.

    Real system paths like '/etc/hosts' are handled by the SYSTEM_CRITICAL zone
    check downstream; this guard only catches the common virtual-path mistake.
    """
    resolved = os.path.normpath(os.path.realpath(os.path.abspath(os.path.expanduser(raw))))
    ws = str(workspace_root.resolve())
    if resolved.startswith(ws):
        return raw  # already correct
    # Heuristic: if the path has ≤ 3 components from root it's likely virtual
    parts = Path(resolved).parts  # ('/', 'README.md') or ('/', 'sub', 'f.py')
    if len(parts) <= 4:
        corrected = str(workspace_root.resolve() / Path(raw).relative_to("/"))
        return corrected
    return raw

# ---------------------------------------------------------------------------
# TaskRunnerAgent registry — one instance per resolved workspace root
# ---------------------------------------------------------------------------

_registry: dict[str, TaskRunnerAgent] = {}
_default_policy: object | None = None  # PermissionsPolicy; set by daemon at boot


def set_default_policy(policy: object | None) -> None:
    """Install a permissions policy for every TaskRunnerAgent the registry mints.

    The daemon calls this once at startup. Already-cached agents are updated
    in place so a hot reload picks up the new policy without rebuilding tools.
    """
    global _default_policy
    _default_policy = policy
    for agent in _registry.values():
        agent._policy = policy  # type: ignore[attr-defined]


def _get_or_create_agent(workspace_root: Path, sandbox_root: Path | None = None) -> TaskRunnerAgent:
    ws = str(workspace_root.resolve())
    sb = str(sandbox_root.resolve()) if sandbox_root is not None else ""
    key = f"{ws}|{sb}"
    if key not in _registry:
        _registry[key] = TaskRunnerAgent(
            workspace_root, sandbox_root=sandbox_root, policy=_default_policy,
        )
    return _registry[key]


# ---------------------------------------------------------------------------
# Tool factory — creates the 4 tools bound to workspace_root
# ---------------------------------------------------------------------------


def bind_tools(workspace_root: Path, sandbox_root: Path | None = None) -> list[BaseTool]:
    """
    Return the TaskRunner tools bound to *workspace_root*.

    The tools are:
      - tr_read_file          — read any file (zone-checked)
      - tr_write_file         — write/create a file (zone-checked)
      - tr_delete_file        — delete a file or directory (zone-checked)
      - tr_execute_in_sandbox — run a shell command inside the sandbox zone
      - tr_ask_user           — ask the user a question and get their text response

    Each file/shell tool returns a JSON string (OperationResponse.model_dump_json())
    so the calling LLM sees a structured, parseable result in its ToolMessage.
    """
    agent = _get_or_create_agent(workspace_root, sandbox_root)

    # ------------------------------------------------------------------ #
    # tr_read_file                                                         #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_read_file(
        path: str,
        reason: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Read a file (zone-checked). Returns JSON with content and pagination metadata.

        For large files use offset + limit to read in chunks:
          - offset: 0-based line number to start from (default 0 = beginning of file).
          - limit:  maximum lines to return per call (omit to read from offset to EOF).

        When the file has more content after the returned chunk, result.has_more is True
        and result.truncation_notice contains recovery hints with the next offset to use.

        Args:
            path:   Absolute real path to the file (convert virtual ls paths first).
            reason: Specific purpose shown to the user in permission prompts.
            offset: Line number to start reading from (0-based, default 0).
            limit:  Maximum number of lines to return (None = read to end of file).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_read_file] path=%s offset=%s limit=%s", real_path, offset, limit)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.READ,
            paths=[real_path],
            reason=reason,
            additional_context={"offset": offset, "limit": limit},
        )
        response = await agent.handle(request)
        _log.debug("[tr_read_file] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_write_file                                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_write_file(path: str, content: str, reason: str) -> str:
        """Write/create a file (zone-checked, creates parent dirs). Returns JSON {status, result: {written_to}, error}.

        Args:
            path: Absolute path to write.
            content: Text content to write.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_write_file] path=%s bytes=%d", real_path, len(content.encode()))
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.WRITE,
            paths=[real_path],
            reason=reason,
            additional_context={"content": content},
        )
        response = await agent.handle(request)
        _log.debug("[tr_write_file] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_delete_file                                                       #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_delete_file(path: str, reason: str) -> str:
        """Delete a file or directory (zone-checked). Returns JSON {status, result: {deleted}, error}.

        Args:
            path: Absolute path to delete.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_delete_file] path=%s", real_path)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.DELETE,
            paths=[real_path],
            reason=reason,
        )
        response = await agent.handle(request)
        _log.debug("[tr_delete_file] status=%s", response.status)
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
        """Run a shell command in the sandbox (auto-allowed, cwd=_sandbox/). Returns JSON {status, result: {stdout, stderr, exit_code}, error}.

        Args:
            command: Shell command to run.
            reason: Why you are running this command.
            timeout: Max seconds (default 120).
        """
        sandbox_path = str(agent.sandbox_root)
        _log.debug("[tr_execute_in_sandbox] cmd=%s", command[:200])
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
        _log.debug("[tr_execute_in_sandbox] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_grep                                                              #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_grep(
        pattern: str,
        path: str,
        reason: str,
        context_lines: int = 3,
        case_insensitive: bool = False,
        max_matches: int = 100,
    ) -> str:
        """Search for a regex pattern in a file or directory using real paths.

        Use this instead of the built-in grep tool, which only works on virtual
        paths and returns no results when given real absolute paths.

        Returns JSON with stdout containing matching lines (with line numbers),
        or a SuppressedContentNotice if the output is too large.

        Args:
            pattern:          Regex or fixed string to search for.
            path:             Real absolute path to a file or directory to search.
            reason:           Specific purpose shown to the user in permission prompts.
            context_lines:    Lines of context before/after each match (default 3).
            case_insensitive: Case-insensitive matching (default False).
            max_matches:      Stop after this many matches (default 100).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_grep] pattern=%r path=%s", pattern, real_path)
        flags = "-rn"
        if case_insensitive:
            flags += "i"
        cmd = (
            f"grep {flags} -C {context_lines} -m {max_matches} "
            f"--color=never -- {json.dumps(pattern)} {json.dumps(real_path)}"
        )
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
                "command": cmd,
                "timeout": 30,
                "cwd": sandbox_path,
            },
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_ask_user                                                          #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_ask_user(
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question and return their text response.

        Use this when you need clarification, a choice between approaches, or
        explicit confirmation before an irreversible action. The question is
        shown to the user on their terminal and their answer is returned.

        Args:
            question: The question to show the user.
            options: Optional list of suggested responses shown as hints (not enforced).

        Returns:
            JSON: {status, result: {response: <user's answer>}}
        """
        payload = {
            "type": "user_question",
            "question": question,
            "options": options or [],
        }
        response: str = interrupt(payload)
        return json.dumps({"status": "success", "result": {"response": response}})

    # ------------------------------------------------------------------ #
    # tr_execute                                                           #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_execute(
        command: str,
        reason: str,
        timeout: int = 120,
    ) -> str:
        """Run a shell command on the host machine (requires user approval each time).

        Unlike tr_execute_in_sandbox (which runs in an isolated sandbox with no
        internet), this tool runs in the workspace directory on the real host.
        It has full network access — use it for curl, wget, API calls, etc.

        Every invocation pauses for user permission before executing. The user
        sees the command and the reason before deciding to approve or deny.

        Args:
            command: Shell command to run (e.g. "curl -s https://example.com").
            reason:  Why you need to run this (shown to user in permission prompt).
            timeout: Max seconds (default 120).
        """
        _log.debug("[tr_execute] cmd=%s", command[:200])
        # Use "/host" as the sentinel path — it is outside workspace and sandbox,
        # so classify_zone() returns EXTERNAL, and EXTERNAL + EXECUTE = PROMPT.
        # This forces a user permission check before every execution.
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent="agent",
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=["/host"],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": str(workspace_root),
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_execute] status=%s", response.status)
        return response.model_dump_json()

    return [tr_read_file, tr_write_file, tr_delete_file, tr_execute_in_sandbox, tr_grep, tr_ask_user, tr_execute]
