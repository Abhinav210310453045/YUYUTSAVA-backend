"""TaskRunner tools: Running commands and scripts.

Split out of the 609-line ``bind_tools`` (`R-RDB-02`); the factory in
``__init__`` assembles the families.
"""

from __future__ import annotations

import logging
import uuid

from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.task_runner.tools.support import (
    ToolContext,
    resolve_path,
)
from yuyutsava.models.operations import OperationRequest, OperationType

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.shell")


def make_shell_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the shell tools bound to one workspace."""
    agent, workspace_root, agent_name = ctx.agent, ctx.workspace_root, ctx.agent_name

    @tool
    async def tr_execute_in_sandbox(
        command: str,
        reason: str,
        timeout: int = 120,
    ) -> str:
        """Run a shell command in the sandbox (auto-allowed, cwd = the sandbox dir). Returns JSON {status, result: {stdout, stderr, exit_code}, error}.

        No network. CWD = sandbox dir (created on demand); use relative paths.
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
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": sandbox_path,
                # Marks the sandbox-cwd shell path so the dispatcher can hand
                # it to the exec backend (docker mode) without path heuristics.
                "sandboxed": True,
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_execute_in_sandbox] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_run_python(script_path: str, reason: str, timeout: int = 120) -> str:
        """Run a Python script in the sandbox with the daemon's own interpreter (auto-allowed).

        PORTABLE — runs identically on Windows/macOS/Linux (uses sys.executable,
        no shell, no quoting). Prefer this over writing a bash/.sh script for ANY
        custom multi-step logic (file wrangling, parsing, batch ops, moving/copying/
        unzipping files). Returns JSON {status, result: {stdout, stderr, exit_code}, error}.

        Lifecycle: tr_write_file('script.py', ...) → tr_run_python('script.py') →
        read result.stdout → tr_delete_file (or keep it under .yuyutsava/scripts/
        when it is reusable). CWD = sandbox dir; reference workspace files by
        absolute path, write outputs with relative paths. Do NOT tr_read_file
        the script you just wrote — read the execution result.

        Args:
            script_path: Path to the .py file to run (absolute, or relative to workspace).
            reason: Why you are running this script.
            timeout: Max seconds (default 120).
        """
        real = resolve_path(script_path, workspace_root)
        sandbox_path = str(agent.sandbox_root)
        _log.debug("[tr_run_python] script=%s", real)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "python": {"script_path": real, "cwd": sandbox_path, "timeout": timeout},
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_run_python] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_execute(
        command: str,
        reason: str,
        timeout: int = 120,
        elevated: bool = False,
    ) -> str:
        """Run a native shell command on the host (workspace cwd, full network, asks every time).

        The command runs in the host's NATIVE shell — PowerShell on Windows,
        bash on macOS/Linux — so write OS-native syntax for the current host
        (call tr_sysinfo if unsure which OS this is). Use this for OS-native
        administration (services, installs, diagnostics) and internet-required
        commands. For portable multi-step logic prefer tr_run_python; for
        local-only work with no network use tr_execute_in_sandbox.

        Set elevated=True for commands needing admin/root — this triggers the OS
        elevation prompt (UAC on Windows, admin auth on macOS, pkexec/sudo on
        Linux). Elevated runs are CRITICAL: the user is asked fresh every time.

        Args:
            command: Native shell command (PowerShell on Windows, bash on POSIX).
            reason:  Why you need to run this (shown to user in permission prompt).
            timeout: Max seconds (default 120).
            elevated: Run with admin/root via the OS elevation prompt.
        """
        _log.debug("[tr_execute] cmd=%s elevated=%s", command[:200], elevated)
        # Use "/host" as the sentinel path — it is outside workspace and sandbox,
        # so classify_zone() returns EXTERNAL, and EXTERNAL + EXECUTE = PROMPT.
        # This forces a user permission check before every execution.
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=["/host"],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": str(workspace_root),
                "elevated": elevated,
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_execute] status=%s", response.status)
        return response.model_dump_json()

    return [
        tr_execute_in_sandbox,
        tr_run_python,
        tr_execute,
    ]
