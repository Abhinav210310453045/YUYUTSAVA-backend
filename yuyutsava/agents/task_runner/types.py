"""
Shared data models and enums for the TaskRunnerAgent.

No business logic lives here — only type definitions used across the sub-package.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class OperationType(str, Enum):
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    EXECUTE = "execute"
    CHMOD = "chmod"


class FilesystemZone(str, Enum):
    SANDBOX = "sandbox"
    WORKSPACE = "workspace"
    EXTERNAL = "external"
    SYSTEM_CRITICAL = "system_critical"


class PermissionAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


class OperationRequest(BaseModel):
    """Request from any agent to perform a filesystem operation."""

    request_id: str
    requesting_agent: str = "deepagent-master"
    parent_agent: str | None = None
    task_id: str
    task_description: str
    operation: OperationType
    paths: list[str]
    reason: str
    additional_context: dict[str, Any] | None = None


class OperationResponse(BaseModel):
    """Standardized response returned by TaskRunnerAgent to the requesting agent."""

    request_id: str
    operation_id: str
    status: Literal["success", "denied", "error"]

    # Populated on success
    result: Any | None = None

    # Populated on denied / error
    error: str | None = None
    error_code: str | None = None  # TR001=path-traversal, TR002=system-critical, TR003=user-denied,
                                   # TR004=rule-denied, TR005=execution-error
    alternatives: list[str] | None = None

    # Context for parent agent / audit
    zone: str | None = None
    operation: str | None = None
