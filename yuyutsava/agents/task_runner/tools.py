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


def _validation_error_json(exc: Exception) -> str:
    """Return a structured JSON error so the LLM sees `status: error` instead of a
    langchain `Error invoking tool ...` string it tends to ignore.
    Attached as ``handle_validation_error=`` on every tr_* @tool so pydantic
    arg-validation failures (missing reason=, wrong type, etc.) become a normal
    OperationResponse-shaped result the model can react to.
    """
    msg = str(exc).replace("\n", " ")
    return json.dumps({
        "status": "error",
        "error_code": "TR000_VALIDATION",
        "error": f"Tool arguments invalid: {msg}",
        "hint": (
            "All tr_* tools require a non-empty `reason` string describing why the "
            "operation is needed. Re-call the tool with every required argument."
        ),
    })


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
        """Read a file (zone-checked). Returns JSON {content, has_more, truncation_notice, total_lines}.

        Paginate large files via offset/limit; result.has_more + result.truncation_notice
        give the next offset. After tr_grep, feed the matched line number as offset.

        Args:
            path:   Absolute real path (convert virtual ls/glob paths first).
            reason: Specific purpose shown to the user in permission prompts.
            offset: 0-based line to start from (default 0).
            limit:  Max lines per call (None = read to EOF).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_read_file] path=%s offset=%s limit=%s", real_path, offset, limit)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            #TODO: Add the agent name who is going to use this tool
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

        First write into the sandbox creates the sandbox dir.
        Deliverables → output_dir (from system prompt); scratch → sandbox.

        Args:
            path: Absolute real path to write.
            content: Text content to write.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_write_file] path=%s bytes=%d", real_path, len(content.encode()))
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            #TODO: Add the agent name who is going to use this tool
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

        Use to clean up temp scripts after tr_execute_in_sandbox.
        WORKSPACE zone prompts the user; SANDBOX is auto-allowed.

        Args:
            path: Absolute real path to delete.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_delete_file] path=%s", real_path)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            #TODO: Add the agent name who is going to use this tool
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

        No network. CWD = sandbox dir; use relative paths.
        Sandbox dir is created by the first tr_write_file — do not call this before any write.
        Script lifecycle: tr_write_file → this → read result.stdout → tr_delete_file.
        Do NOT tr_read_file a script you just wrote; read the execution result.

        Args:
            command: Shell command to run.
            reason: Why you are running this command.
            timeout: Max seconds (default 120).
        """
        sandbox_path = str(agent.sandbox_root)
        _log.debug("[tr_execute_in_sandbox] cmd=%s", command[:200])
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            #TODO: Add the agent name who is going to use this tool
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
        """Search a regex pattern in a file or directory. Returns JSON with stdout (matches + line numbers).

        Use this, NOT the built-in grep (which only works on virtual paths).
        Pass real absolute paths; returned line numbers feed tr_read_file offset.

        Args:
            pattern:          Regex or fixed string to search for.
            path:             Real absolute path to a file or directory.
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
            #TODO: Add the agent name who is going to use this tool
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
        from yuyutsava.core.agent_context import current_context

        payload = {
            "type": "user_question",
            "question": question,
            "options": options or [],
            **current_context(),
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
        """Run a shell command on the host (workspace cwd, full network, asks user every time).

        Use for internet-required commands (curl/wget/API). For local-only,
        use tr_execute_in_sandbox (no approval prompt, no network).

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

    all_tools: list[BaseTool] = [
        tr_read_file, tr_write_file, tr_delete_file,
        tr_execute_in_sandbox, tr_grep, tr_ask_user, tr_execute,
    ]
    # Convert pydantic arg-validation failures (missing reason=, wrong type, etc.)
    # into a structured JSON ToolMessage instead of langchain's opaque
    # "Error invoking tool ..." string the LLM tends to ignore.
    for t in all_tools:
        t.handle_validation_error = _validation_error_json
    return all_tools
