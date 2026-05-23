"""
Core operation models and enums for the TaskRunnerAgent permission gateway.

These are the primary schemas shared across the entire system:
  - OperationType    — what kind of filesystem operation is requested
  - FilesystemZone   — which security zone a path falls into
  - PermissionAction — what the rule engine decides to do
  - OperationRequest — structured request from any agent to the gateway
  - OperationResponse — structured response returned in all cases (success/denied/error)
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel, Field

from yuyutsava.models.results import DeleteResult, ListResult, ReadResult, ShellResult, WriteResult


class OperationType(str, Enum):
    READ    = "read"
    WRITE   = "write"
    CREATE  = "create"
    DELETE  = "delete"
    EXECUTE = "execute"
    CHMOD   = "chmod"
    LIST    = "list"   # directory listing (tr_ls)
    GLOB    = "glob"   # pattern match (tr_glob)


class FilesystemZone(str, Enum):
    SANDBOX         = "sandbox"
    WORKSPACE       = "workspace"
    EXTERNAL        = "external"
    SYSTEM_CRITICAL = "system_critical"


class PermissionAction(str, Enum):
    ALLOW  = "allow"
    DENY   = "deny"
    PROMPT = "prompt"


# Union of all concrete result types — replaces bare `Any` on OperationResponse.result
OperationResult = Union[ShellResult, WriteResult, DeleteResult, ReadResult, ListResult, None]


class OperationRequest(BaseModel):
    """Request from any agent to perform a filesystem operation."""

    request_id:         str
    requesting_agent:   str = "deepagent-master"
    parent_agent:       str | None = None
    task_id:            str
    task_description:   str
    operation:          OperationType
    paths:              list[str]
    reason:             str
    # Carries operation-specific extras: content (write), command/timeout/cwd (execute)
    additional_context: dict[str, object] | None = None


class OperationResponse(BaseModel):
    """Standardised response returned by TaskRunnerAgent in all cases."""

    request_id:   str
    operation_id: str
    status:       Literal["success", "denied", "error"]

    # Populated on success — typed union instead of bare Any
    result: OperationResult = None

    # Populated on denied / error
    error:      str | None = None
    error_code: str | None = None  # TR001=path-traversal  TR002=system-critical
                                   # TR003=user-denied      TR004=rule-denied
                                   # TR005=execution-error
    alternatives: list[str] | None = None

    # Context for the calling agent and audit log
    zone:      str | None = None
    operation: str | None = None
