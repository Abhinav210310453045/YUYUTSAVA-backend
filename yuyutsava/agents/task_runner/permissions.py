"""
Permission rule engine for the TaskRunnerAgent.

Pure functions only — no side effects, no I/O, no LangGraph imports.
The rule table is the single source of truth for all zone/operation decisions.
"""

from __future__ import annotations

from yuyutsava.models.interrupts import TaskRunnerPermissionInterrupt
from yuyutsava.models.operations import (
    FilesystemZone,
    OperationRequest,
    OperationType,
    PermissionAction,
)

# ---------------------------------------------------------------------------
# Rule table: (zone, operation) → action
# Default for any unlisted combination: DENY (fail-safe)
# ---------------------------------------------------------------------------

_RULES: dict[tuple[FilesystemZone, OperationType], PermissionAction] = {
    # ── SANDBOX: all operations auto-allowed ─────────────────────────────
    (FilesystemZone.SANDBOX, OperationType.READ):    PermissionAction.ALLOW,
    (FilesystemZone.SANDBOX, OperationType.WRITE):   PermissionAction.ALLOW,
    (FilesystemZone.SANDBOX, OperationType.CREATE):  PermissionAction.ALLOW,
    (FilesystemZone.SANDBOX, OperationType.DELETE):  PermissionAction.ALLOW,
    (FilesystemZone.SANDBOX, OperationType.EXECUTE): PermissionAction.ALLOW,
    (FilesystemZone.SANDBOX, OperationType.CHMOD):   PermissionAction.ALLOW,

    # ── WORKSPACE: read/write/create auto-allowed; delete prompts; rest denied
    (FilesystemZone.WORKSPACE, OperationType.READ):    PermissionAction.ALLOW,
    (FilesystemZone.WORKSPACE, OperationType.WRITE):   PermissionAction.ALLOW,
    (FilesystemZone.WORKSPACE, OperationType.CREATE):  PermissionAction.ALLOW,
    (FilesystemZone.WORKSPACE, OperationType.DELETE):  PermissionAction.PROMPT,
    (FilesystemZone.WORKSPACE, OperationType.EXECUTE): PermissionAction.DENY,
    (FilesystemZone.WORKSPACE, OperationType.CHMOD):   PermissionAction.DENY,

    # ── EXTERNAL: all operations require user confirmation ────────────────
    (FilesystemZone.EXTERNAL, OperationType.READ):    PermissionAction.PROMPT,
    (FilesystemZone.EXTERNAL, OperationType.WRITE):   PermissionAction.PROMPT,
    (FilesystemZone.EXTERNAL, OperationType.CREATE):  PermissionAction.PROMPT,
    (FilesystemZone.EXTERNAL, OperationType.DELETE):  PermissionAction.PROMPT,
    (FilesystemZone.EXTERNAL, OperationType.EXECUTE): PermissionAction.PROMPT,
    (FilesystemZone.EXTERNAL, OperationType.CHMOD):   PermissionAction.PROMPT,

    # ── SYSTEM_CRITICAL: always denied ───────────────────────────────────
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.READ):    PermissionAction.DENY,
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.WRITE):   PermissionAction.DENY,
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.CREATE):  PermissionAction.DENY,
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.DELETE):  PermissionAction.DENY,
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.EXECUTE): PermissionAction.DENY,
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.CHMOD):   PermissionAction.DENY,
}

# ---------------------------------------------------------------------------
# Risk levels (for HITL prompt display and audit logging)
# ---------------------------------------------------------------------------

_RISK_LEVELS: dict[tuple[FilesystemZone, OperationType], str] = {
    (FilesystemZone.SANDBOX,         OperationType.READ):    "LOW",
    (FilesystemZone.SANDBOX,         OperationType.WRITE):   "LOW",
    (FilesystemZone.SANDBOX,         OperationType.CREATE):  "LOW",
    (FilesystemZone.SANDBOX,         OperationType.DELETE):  "LOW",
    (FilesystemZone.SANDBOX,         OperationType.EXECUTE): "LOW",
    (FilesystemZone.WORKSPACE,       OperationType.READ):    "LOW",
    (FilesystemZone.WORKSPACE,       OperationType.WRITE):   "LOW",
    (FilesystemZone.WORKSPACE,       OperationType.CREATE):  "LOW",
    (FilesystemZone.WORKSPACE,       OperationType.DELETE):  "MEDIUM",
    (FilesystemZone.EXTERNAL,        OperationType.READ):    "LOW",
    (FilesystemZone.EXTERNAL,        OperationType.WRITE):   "MEDIUM",
    (FilesystemZone.EXTERNAL,        OperationType.CREATE):  "MEDIUM",
    (FilesystemZone.EXTERNAL,        OperationType.DELETE):  "HIGH",
    (FilesystemZone.EXTERNAL,        OperationType.EXECUTE): "CRITICAL",
    (FilesystemZone.EXTERNAL,        OperationType.CHMOD):   "CRITICAL",
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.READ):    "CRITICAL",
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.WRITE):   "CRITICAL",
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.DELETE):  "CRITICAL",
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.EXECUTE): "CRITICAL",
}

# ---------------------------------------------------------------------------
# Alternatives — suggested workarounds shown to the agent on denial
# ---------------------------------------------------------------------------

_ALTERNATIVES: dict[tuple[FilesystemZone, OperationType], list[str]] = {
    (FilesystemZone.WORKSPACE, OperationType.EXECUTE): [
        "Copy the script to workspace_root/_sandbox/ and use tr_execute_in_sandbox instead.",
    ],
    (FilesystemZone.WORKSPACE, OperationType.CHMOD): [
        "Permission changes are not allowed in the workspace zone.",
        "Use the sandbox zone if chmod is required for a script.",
    ],
    (FilesystemZone.SYSTEM_CRITICAL, OperationType.READ): [
        "System-critical paths are always protected. No alternative access is available.",
    ],
    (FilesystemZone.EXTERNAL, OperationType.READ): [
        "Ask the user to copy the file to the workspace first.",
        "Use tr_read_file with a workspace path if a copy is available.",
    ],
    (FilesystemZone.EXTERNAL, OperationType.WRITE): [
        "Write to the workspace instead and notify the user of the path.",
    ],
    (FilesystemZone.EXTERNAL, OperationType.DELETE): [
        "Move the file to workspace trash instead of permanent deletion.",
        "Ask the user to delete the file manually.",
    ],
    (FilesystemZone.EXTERNAL, OperationType.EXECUTE): [
        "Copy the script to workspace_root/_sandbox/ and use tr_execute_in_sandbox.",
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decide(zone: FilesystemZone, operation: OperationType) -> PermissionAction:
    """Return the required permission action for a zone + operation pair.

    Defaults to DENY for any combination not in the rule table (fail-safe).
    """
    return _RULES.get((zone, operation), PermissionAction.DENY)


def get_risk_level(zone: FilesystemZone, operation: OperationType) -> str:
    """Return a human-readable risk label for display in HITL prompts."""
    return _RISK_LEVELS.get((zone, operation), "CRITICAL")


def get_alternatives(zone: FilesystemZone, operation: OperationType) -> list[str]:
    """Return suggested workarounds when an operation is denied or user rejects."""
    return _ALTERNATIVES.get((zone, operation), [])


def build_interrupt_payload(
    request: OperationRequest,
    zone: FilesystemZone,
) -> TaskRunnerPermissionInterrupt:
    """Build the typed interrupt payload passed to LangGraph's ``interrupt()``."""
    from yuyutsava.core.agent_context import current_context

    ctx = current_context()
    parent_path = ctx.get("agent_path") or "orchestrator"
    # If a subagent (anything other than the default "agent" sentinel) requested
    # the op, append its name so the UI shows e.g. "orchestrator/file-organizer"
    # rather than the orchestrator's path. The check is idempotent: re-nesting is
    # avoided so repeated calls within one subagent stay shallow.
    asker = request.requesting_agent
    if asker and asker != "agent" and not parent_path.endswith(f"/{asker}"):
        agent_path = f"{parent_path}/{asker}"
    else:
        agent_path = parent_path
    return TaskRunnerPermissionInterrupt(
        operation=request.operation.value,
        paths=request.paths,
        zone=zone.value,
        reason=request.reason,
        requesting_agent=request.requesting_agent,
        parent_agent=request.parent_agent,
        task_id=request.task_id,
        task_description=request.task_description,
        risk_level=get_risk_level(zone, request.operation),
        session_id=ctx.get("session_id"),
        agent_path=agent_path,
    )
