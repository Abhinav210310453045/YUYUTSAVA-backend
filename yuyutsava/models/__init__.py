"""
yuyutsava.models — canonical schema layer for all structured data in the system.

Sub-modules:
  operations    — OperationRequest, OperationResponse, and the core enums
  results       — typed result payloads that replace `result: Any` in OperationResponse
  interrupts    — typed interrupt payloads passed to LangGraph's interrupt()
  tool_messages — standardized structured messages embedded in tool results
                  (SuppressedContentNotice, RecoveryHint, and future notice types)
"""

from yuyutsava.models.operations import (
    FilesystemZone,
    OperationRequest,
    OperationResponse,
    OperationType,
    PermissionAction,
)
from yuyutsava.models.results import (
    DeleteResult,
    ReadResult,
    ShellResult,
    WriteResult,
)
from yuyutsava.models.interrupts import (
    PermissionRequestInterrupt,
    TaskRunnerPermissionInterrupt,
    UserQuestionInterrupt,
)
from yuyutsava.models.tool_messages import (
    RecoveryHint,
    SuppressedContentNotice,
    SuppressedReason,
    ToolNotice,
    is_tool_notice,
)

__all__ = [
    # operations
    "FilesystemZone",
    "OperationRequest",
    "OperationResponse",
    "OperationType",
    "PermissionAction",
    # results
    "DeleteResult",
    "ReadResult",
    "ShellResult",
    "WriteResult",
    # interrupts
    "PermissionRequestInterrupt",
    "TaskRunnerPermissionInterrupt",
    "UserQuestionInterrupt",
    # tool_messages
    "RecoveryHint",
    "SuppressedContentNotice",
    "SuppressedReason",
    "ToolNotice",
    "is_tool_notice",
]
