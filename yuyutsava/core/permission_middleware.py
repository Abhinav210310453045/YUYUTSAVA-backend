"""
Permission middleware for YUYUTSAVA — fallback safety layer for raw ``execute`` calls.

Two independent checks run on every ``execute`` tool call:

  1. PATH SCOPE CHECK (hard rules, workspace-aware)
     Extracts absolute paths from the command and classifies each one:
       • SYSTEM_CRITICAL path  → HARD BLOCK, no user prompt, ever
       • EXTERNAL path (outside workspace) + destructive command → PROMPT user
       • PROTECTED subdir (.venv, .git, __pycache__ …) + destructive → PROMPT user

  2. PATTERN CHECK (regex, context-free)
     Catches dangerous command shapes regardless of path:
       • rm -rf, sudo, kill -9, find -delete, curl | bash, etc.
       • Enriches reason with protected-dir names when relevant.

Checks run in this order: scope check first (stronger), then pattern check.
The first match that requires user input calls ``interrupt()``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.types import interrupt

from yuyutsava.models.interrupts import PermissionRequestInterrupt
from yuyutsava.platform import host_profile

# ---------------------------------------------------------------------------
# Dangerous-command pattern detection (pattern check).
# Two tables — the active one is selected by the host OS (POSIX shells vs
# PowerShell/cmd) so Windows-destructive shapes are caught natively.
# ---------------------------------------------------------------------------

_POSIX_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # find-based deletion (added previously)
    (re.compile(r"\bfind\b.*-delete\b", re.IGNORECASE), "find -delete (bulk file deletion)"),
    (re.compile(r"\bfind\b.*-exec\s+(rm|unlink)\b", re.IGNORECASE), "find -exec rm/unlink (bulk file deletion)"),
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

# Windows / PowerShell destructive shapes (native admin surface).
_WINDOWS_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bformat\b\s+[a-z]:", re.IGNORECASE), "Disk format"),
    (re.compile(r"\bdel\b.*\s/[sq]", re.IGNORECASE), "Recursive/forced delete (del /s /q)"),
    (re.compile(r"\brmdir\b.*\s/s", re.IGNORECASE), "Recursive directory delete (rmdir /s)"),
    (re.compile(r"Remove-Item\b.*-Recurse\b.*-Force\b", re.IGNORECASE), "PowerShell recursive force delete"),
    (re.compile(r"\breg\b\s+delete\b.*HK(LM|EY_LOCAL_MACHINE)", re.IGNORECASE), "Registry delete (HKLM)"),
    (re.compile(r"\bbcdedit\b", re.IGNORECASE), "Boot configuration edit (bcdedit)"),
    (re.compile(r"\bvssadmin\b.*delete\b.*shadows", re.IGNORECASE), "Delete volume shadow copies"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "Disk partitioning (diskpart)"),
    (re.compile(r"Set-ExecutionPolicy\b.*(Bypass|Unrestricted)", re.IGNORECASE), "Weaken PowerShell execution policy"),
    (re.compile(r"(iex|Invoke-Expression)\b.*(Invoke-WebRequest|iwr|curl|DownloadString)", re.IGNORECASE),
     "Remote code execution (IEX from web)"),
    (re.compile(r"\b(Stop-Computer|Restart-Computer|shutdown)\b", re.IGNORECASE), "System shutdown or restart"),
    (re.compile(r"\btakeown\b|\bicacls\b.*\/grant", re.IGNORECASE), "Ownership / ACL change"),
]


def _dangerous_patterns() -> list[tuple[re.Pattern[str], str]]:
    """The dangerous-pattern table for the current host OS."""
    return _WINDOWS_DANGEROUS_PATTERNS if host_profile().is_windows else _POSIX_DANGEROUS_PATTERNS

# Protected subdirectories inside the workspace — deletion/modification triggers a prompt
_PROTECTED_SUBDIRS: frozenset[str] = frozenset({
    ".venv", ".git", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache",
})

_PROTECTED_SUBDIR_RE: re.Pattern[str] = re.compile(
    r"(?:^|[\s/])(" + "|".join(re.escape(d) for d in sorted(_PROTECTED_SUBDIRS)) + r")(?:/|[\s;|]|$)",
    re.IGNORECASE,
)

def _affected_protected_subdirs(command: str) -> list[str]:
    found: set[str] = set()
    for m in _PROTECTED_SUBDIR_RE.finditer(command):
        found.add(m.group(1).lower())
    return sorted(found)


def classify_command(command: str) -> str | None:
    """Return a human-readable reason if *command* matches a dangerous pattern, else ``None``.

    When a match is found and the command references protected subdirectories,
    the reason is enriched with their names.
    """
    for pattern, reason in _dangerous_patterns():
        if pattern.search(command):
            protected = _affected_protected_subdirs(command)
            if protected:
                return f"{reason} — affects protected directories: {', '.join(protected)}"
            return reason
    return None


# ---------------------------------------------------------------------------
# Path scope check (hard rules)
# ---------------------------------------------------------------------------

# Absolute paths extracted from shell commands — matches /something that is
# not inside a flag like --flag or is not a redirect target already caught above.
_ABS_PATH_RE: re.Pattern[str] = re.compile(
    r"""(?:^|[\s=,;'"`(])(/(?:[^\s'"`|;&)<>\\]+))""",
    re.MULTILINE,
)

# System-critical prefixes — hard block, no user prompt. Per-OS via HostProfile.
def _system_critical_prefixes() -> tuple[str, ...]:
    return host_profile().system_critical_prefixes

# Commands that modify or delete filesystem state
_DESTRUCTIVE_COMMAND_RE: re.Pattern[str] = re.compile(
    r"\b(rm|rmdir|unlink|shred|truncate|mv|dd|mkfs|chmod|chown)\b"
    r"|\bfind\b.*(-delete|-exec\s+(rm|unlink))"
    r"|>\s*/",   # redirect overwrite to an absolute path
    re.IGNORECASE,
)


def _extract_absolute_paths(command: str) -> list[str]:
    """Extract candidate absolute paths from a shell command string."""
    return [m.group(1) for m in _ABS_PATH_RE.finditer(command)]


def _resolve(path: str) -> str:
    """Canonicalize a path without requiring it to exist."""
    return os.path.normpath(os.path.realpath(os.path.abspath(path)))


def _is_system_critical(canonical: str) -> bool:
    """Return True if the canonical path is inside a system-critical directory.

    Case/separator-normalized so it holds on Windows (``C:\\Windows`` with
    backslashes, case-insensitive) as well as POSIX.
    """
    ncanon = os.path.normcase(canonical)
    for prefix in _system_critical_prefixes():
        # check the raw prefix AND its resolved form (macOS /etc → /private/etc)
        for candidate in (prefix, _resolve(prefix)):
            npre = os.path.normcase(candidate)
            if ncanon == npre or ncanon.startswith(npre + os.sep):
                return True
    return False


def _is_outside_workspace(canonical: str, workspace: str) -> bool:
    """Return True if the canonical path is outside the workspace root."""
    try:
        Path(canonical).relative_to(workspace)
        return False
    except ValueError:
        return True


def scope_check(
    command: str,
    workspace_root: Path,
) -> tuple[str, bool] | None:
    """
    Check whether *command* accesses paths outside its allowed scope.

    Returns a ``(reason, hard_block)`` tuple when a violation is found:
      • ``hard_block=True``  → return [BLOCKED] immediately, no user prompt
      • ``hard_block=False`` → pause and ask the user via ``interrupt()``

    Returns ``None`` when no scope violation is detected.

    Rules (evaluated in order — first match wins):
      1. Any path in SYSTEM_CRITICAL          → hard block (always)
      2. Any path OUTSIDE workspace           → prompt user (external scope)
      3. Destructive command + protected dir  → prompt user (protected subdir)
    """
    ws = str(workspace_root.resolve())
    paths = _extract_absolute_paths(command)

    for raw in paths:
        canonical = _resolve(raw)

        # Rule 1 — system-critical: hard block, no question asked
        if _is_system_critical(canonical):
            return (
                f"Command accesses system-critical path '{raw}' "
                f"({canonical}). This is always blocked.",
                True,  # hard_block
            )

    is_destructive = bool(_DESTRUCTIVE_COMMAND_RE.search(command))

    for raw in paths:
        canonical = _resolve(raw)

        # Rule 2 — outside workspace: prompt user
        if _is_outside_workspace(canonical, ws):
            action_desc = "modify/delete" if is_destructive else "access"
            return (
                f"Command attempts to {action_desc} a path outside the workspace: "
                f"'{raw}' (resolved: {canonical}). "
                f"Workspace boundary: {ws}",
                False,  # prompt, not hard block
            )

    # Rule 3 — destructive command touching protected subdirs inside workspace
    if is_destructive:
        protected = _affected_protected_subdirs(command)
        if protected:
            dirs_str = ", ".join(protected)
            return (
                f"Destructive command affects protected directories inside the workspace: "
                f"{dirs_str}. These directories should not be modified by agents.",
                False,  # prompt user
            )

    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class PermissionMiddleware(AgentMiddleware):  # type: ignore[misc]
    """
    Async-only middleware that intercepts the ``execute`` tool call.

    Acts as the fallback safety layer when the LLM calls ``execute`` directly
    instead of routing through the TaskRunnerAgent tr_* tools.

    Two checks run in sequence:
      1. Scope check  — path-based hard rules (workspace boundary + system-critical)
      2. Pattern check — regex detection of dangerous command shapes

    For each check:
      • Hard block  → return [BLOCKED] ToolMessage immediately, no user prompt
      • Soft block  → call ``interrupt()`` and wait for user approval via stdin
      • No match    → pass through to the next check or allow execution
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root.resolve() if workspace_root else None

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Any,
    ) -> Any:
        tool_name: str = request.tool_call.get("name", "")

        if tool_name == "execute":
            args: dict[str, Any] = request.tool_call.get("args", {})
            command: str = args.get("command", "") if isinstance(args, dict) else ""
            tool_call_id: str = request.tool_call.get("id", "") or ""

            # ── Check 1: Path scope (hard rules) ─────────────────────────
            if self.workspace_root is not None:
                violation = scope_check(command, self.workspace_root)
                if violation is not None:
                    scope_reason, hard_block = violation

                    if hard_block:
                        # System-critical path: block immediately, no user prompt
                        return ToolMessage(
                            content=(
                                f"[BLOCKED] Access denied — system-critical path.\n"
                                f"Command : {command}\n"
                                f"Reason  : {scope_reason}"
                            ),
                            tool_call_id=tool_call_id,
                            name=tool_name,
                        )

                    # Out-of-workspace or protected dir: ask user
                    decision: str = interrupt(
                        PermissionRequestInterrupt(
                            command=command, reason=scope_reason
                        ).to_interrupt_dict()
                    )
                    if decision != "approve":
                        return ToolMessage(
                            content=(
                                f"[BLOCKED] User denied permission.\n"
                                f"Command : {command}\n"
                                f"Reason  : {scope_reason}"
                            ),
                            tool_call_id=tool_call_id,
                            name=tool_name,
                        )

            # ── Check 2: Dangerous-command patterns (regex) ───────────────
            pattern_reason = classify_command(command)
            if pattern_reason:
                decision = interrupt(
                    PermissionRequestInterrupt(
                        command=command, reason=pattern_reason
                    ).to_interrupt_dict()
                )
                if decision != "approve":
                    return ToolMessage(
                        content=(
                            f"[BLOCKED] User denied permission to run this command.\n"
                            f"Command : {command}\n"
                            f"Reason  : {pattern_reason}"
                        ),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )

        return await handler(request)
