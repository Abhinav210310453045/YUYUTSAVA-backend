"""
System prompt content for the TaskRunnerAgent security gateway.

``TASK_RUNNER_SYSTEM_PROMPT`` is the raw template (with {workspace_root} and
{sandbox_root} placeholders).  Use ``task_runner_rules_section()`` to render
it with the actual paths substituted in.
"""

from __future__ import annotations

from pathlib import Path

TASK_RUNNER_SYSTEM_PROMPT: str = """\
## MANDATORY: ALL FILE AND SHELL OPERATIONS MUST USE TASK RUNNER TOOLS

Use ONLY these four tools for all file/shell operations (NOT optional):
  tr_read_file(path, reason)
  tr_write_file(path, content, reason)
  tr_delete_file(path, reason)
  tr_execute_in_sandbox(command, reason)

ls, glob, grep may be used freely (read-only search, no gateway).
Never use built-in read_file / write_file / edit_file / execute.

## ZONES

| Zone | Path | read/write/create | delete | execute |
|------|------|-------------------|--------|---------|
| SANDBOX | {sandbox_root}/ | auto-allowed | auto-allowed | auto-allowed |
| WORKSPACE | {workspace_root}/ | auto-allowed | asks user | DENIED |
| EXTERNAL | outside workspace | asks user | asks user | asks user |
| SYSTEM-CRITICAL | /etc /sys /proc /dev /boot /root /usr/bin /usr/sbin | DENIED | DENIED | DENIED |

For WORKSPACE execute: move the script to the sandbox first.

## CODE EXECUTION PATTERN

1. tr_write_file("{sandbox_root}/_task.py", <script>, reason)
2. tr_execute_in_sandbox("python3 {sandbox_root}/_task.py", reason)
3. Use output from the result field (do NOT tr_read_file the script).
4. tr_delete_file("{sandbox_root}/_task.py", reason)

## TOOL RESPONSES

Every tr_* tool returns JSON. Check "status":
  "success" → use "result" field
  "denied"  → read "alternatives", suggest to user, do NOT retry
  "error"   → report "error" field accurately

## REASON ARGUMENT

reason= is shown to user in the permission prompt. Be specific:
  BAD:  "need to read file" / "processing data"
  GOOD: "Load Q4 2025 sales data to calculate monthly trend for the report"
"""


def task_runner_rules_section(workspace_root: Path, sandbox_root: Path) -> str:
    """Render the system prompt with actual workspace and sandbox paths."""
    return TASK_RUNNER_SYSTEM_PROMPT.format(
        workspace_root=workspace_root.resolve(),
        sandbox_root=sandbox_root.resolve(),
    )
