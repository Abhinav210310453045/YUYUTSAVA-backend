"""
Permission middleware for YUYUTSAVA.

Intercepts ``execute`` tool calls, checks them against a list of dangerous
command patterns, and calls ``interrupt()`` to pause the LangGraph execution
so the user can approve or reject the command before it runs.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import ToolMessage
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.types import interrupt

# ---------------------------------------------------------------------------
# Dangerous-command detection
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Recursive / forced deletion
    (re.compile(r"\brm\s+.*-[^\s]*r", re.IGNORECASE), "Recursive file deletion"),
    (re.compile(r"\brm\s+.*-[^\s]*f", re.IGNORECASE), "Forced file deletion"),
    # Privilege escalation
    (re.compile(r"\bsudo\b"), "Privilege escalation (sudo)"),
    (re.compile(r"\bsu\s"), "Switch user (su)"),
    # System control
    (re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b"), "System shutdown or reboot"),
    # Disk / filesystem
    (re.compile(r"\bdd\s+if="), "Direct disk write (dd)"),
    (re.compile(r"\bmkfs\b"), "Filesystem creation (mkfs)"),
    # Process termination
    (re.compile(r"\bkill\s+-[^\s]*9"), "SIGKILL (kill -9)"),
    (re.compile(r"\bpkill\b"), "Mass process kill (pkill)"),
    (re.compile(r"\bkillall\b"), "Mass process kill (killall)"),
    # Arbitrary code from network
    (re.compile(r"curl\b.*[|;]\s*(sh|bash|zsh)\b"), "Remote code execution via curl"),
    (re.compile(r"wget\b.*[|;]\s*(sh|bash|zsh)\b"), "Remote code execution via wget"),
    # Scheduled tasks
    (re.compile(r"\bcrontab\b"), "Crontab modification"),
    # Broad permission changes
    (re.compile(r"\bchmod\s+[0-7]*[7]\b"), "World-writable permission change"),
    (re.compile(r"\bchown\b"), "File ownership change"),
    # Writing outside workspace to system paths
    (re.compile(r">\s*/etc/"), "Write to /etc"),
    (re.compile(r">\s*/usr/"), "Write to /usr"),
    (re.compile(r">\s*/(bin|sbin|lib)/"), "Write to system binary directory"),
]


def classify_command(command: str) -> str | None:
    """Return a human-readable reason if *command* matches a dangerous pattern, else ``None``."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return reason
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class PermissionMiddleware(AgentMiddleware):  # type: ignore[misc]
    """
    Async-only middleware that intercepts the ``execute`` tool call.

    If the command matches a dangerous pattern, ``interrupt()`` is called to
    pause the LangGraph graph and surface a permission request to the caller.
    The caller must resume with ``Command(resume="approve")`` or
    ``Command(resume="reject")``.

    If approved (or not dangerous), the tool runs normally.
    If rejected, a ``ToolMessage`` with a ``[BLOCKED]`` body is returned
    without executing the command.
    """

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Any,
    ) -> Any:
        tool_name: str = request.tool_call.get("name", "")

        if tool_name == "execute":
            args: dict[str, Any] = request.tool_call.get("args", {})
            command: str = args.get("command", "") if isinstance(args, dict) else ""
            reason = classify_command(command)

            if reason:
                decision: str = interrupt(
                    {
                        "type": "permission_request",
                        "command": command,
                        "reason": reason,
                    }
                )
                if decision != "approve":
                    tool_call_id: str = request.tool_call.get("id", "") or ""
                    return ToolMessage(
                        content=(
                            f"[BLOCKED] User denied permission to run this command.\n"
                            f"Command : {command}\n"
                            f"Reason  : {reason}"
                        ),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )

        return await handler(request)
