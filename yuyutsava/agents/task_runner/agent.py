"""
TaskRunnerAgent — the central permission gateway for filesystem operations.

Every request goes through ``handle()``:
  1. Canonicalize paths
  2. Classify zone
  3. Apply permission rule
  4. DENY  → return denied response immediately (no user prompt)
  5. PROMPT → call LangGraph interrupt() and wait for user decision
  6. ALLOW / user-approved → execute via executor
  7. Log outcome and return OperationResponse

This class has no awareness of HTTP, CLI, or LangChain specifics.
It only imports from within this sub-package plus langgraph.types.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from langgraph.types import interrupt

from yuyutsava.agents.task_runner import executor as _exec
from yuyutsava.agents.task_runner.permissions import (
    build_interrupt_payload,
    decide,
    get_alternatives,
    get_risk_level,
)
from yuyutsava.agents.task_runner.zones import classify_zone
from yuyutsava.models.operations import (
    FilesystemZone,
    OperationRequest,
    OperationResponse,
    OperationType,
    PermissionAction,
)
from yuyutsava.models.results import DeleteResult, ReadResult, ShellResult, WriteResult
from yuyutsava.models.tool_messages import SuppressedContentNotice

logger = logging.getLogger("yuyutsava.task_runner")

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

_EC_PATH_TRAVERSAL = "TR001"
_EC_SYSTEM_CRITICAL = "TR002"
_EC_USER_DENIED     = "TR003"
_EC_RULE_DENIED     = "TR004"
_EC_EXEC_ERROR      = "TR005"


class TaskRunnerAgent:
    """
    Permission gateway for all filesystem operations.

    Instantiate once per workspace and share across all agents that need
    access to the filesystem (DeepAgent + any sub-agents).
    """

    def __init__(
        self,
        workspace_root: Path,
        sandbox_subdir: str = "_sandbox",
        sandbox_root: Path | None = None,
    ) -> None:
        self.workspace_root: Path = workspace_root.resolve()
        self.sandbox_root: Path = (
            sandbox_root.resolve() if sandbox_root is not None
            else (self.workspace_root / sandbox_subdir).resolve()
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle(self, request: OperationRequest) -> OperationResponse:
        """
        Process a filesystem operation request end-to-end.

        Returns an ``OperationResponse`` in ALL cases (success, denied, error).
        Never raises exceptions — all errors are captured in the response.
        """
        operation_id = str(uuid.uuid4())

        # We process one primary path for zone detection.
        # Multi-path requests use the first path for zone; each path is canonicalized.
        if not request.paths:
            return self._denied(
                request, operation_id,
                error="No paths specified in request.",
                error_code=_EC_RULE_DENIED,
                zone=None,
            )

        primary_path = request.paths[0]

        # 1. Classify zone (canonicalizes internally)
        zone = classify_zone(primary_path, self.workspace_root, self.sandbox_root)

        # 2. Apply rule table
        action = decide(zone, request.operation)

        logger.debug(
            "TaskRunner | %s | zone=%s | action=%s | path=%s",
            request.operation.value.upper(), zone.value, action.value, primary_path,
        )

        # 3. DENY — system-critical or rule-denied, no user prompt
        if action == PermissionAction.DENY:
            if zone == FilesystemZone.SYSTEM_CRITICAL:
                error_msg = (
                    f"Access denied: '{primary_path}' is a system-critical path "
                    f"({zone.value}). All operations on this zone are always blocked."
                )
                code = _EC_SYSTEM_CRITICAL
            else:
                error_msg = (
                    f"Operation '{request.operation.value}' is not permitted in the "
                    f"'{zone.value}' zone."
                )
                code = _EC_RULE_DENIED

            self._log_denied(request, zone, action, error_msg)
            return self._denied(
                request, operation_id,
                error=error_msg,
                error_code=code,
                zone=zone,
                alternatives=get_alternatives(zone, request.operation),
            )

        # 4. PROMPT — ask the user via LangGraph interrupt()
        if action == PermissionAction.PROMPT:
            payload = build_interrupt_payload(request, zone)
            decision: str = interrupt(payload.to_interrupt_dict())

            if decision != "approve":
                error_msg = (
                    f"User denied permission for {request.operation.value.upper()} "
                    f"on '{primary_path}'."
                )
                self._log_denied(request, zone, action, error_msg, user_decision="reject")
                return self._denied(
                    request, operation_id,
                    error=error_msg,
                    error_code=_EC_USER_DENIED,
                    zone=zone,
                    alternatives=get_alternatives(zone, request.operation),
                )

            logger.info(
                "TaskRunner | APPROVED by user | %s | %s",
                request.operation.value.upper(), primary_path,
            )

        # 5. ALLOW (or user-approved) — execute
        try:
            result = await self._execute(request)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("TaskRunner | EXEC ERROR | %s", error_msg)
            return OperationResponse(
                request_id=request.request_id,
                operation_id=operation_id,
                status="error",
                error=error_msg,
                error_code=_EC_EXEC_ERROR,
                zone=zone.value if zone else None,
                operation=request.operation.value,
            )

        logger.info(
            "TaskRunner | SUCCESS | %s | zone=%s | path=%s",
            request.operation.value.upper(), zone.value if zone else "?", primary_path,
        )
        return OperationResponse(
            request_id=request.request_id,
            operation_id=operation_id,
            status="success",
            result=result,
            zone=zone.value if zone else None,
            operation=request.operation.value,
        )

    # ------------------------------------------------------------------
    # Execution dispatch
    # ------------------------------------------------------------------

    async def _execute(
        self, request: OperationRequest
    ) -> ShellResult | WriteResult | DeleteResult | ReadResult:
        """Dispatch to the appropriate executor and return a typed result model."""
        path = Path(request.paths[0])
        ctx = request.additional_context or {}

        match request.operation:
            case OperationType.READ:
                offset = int(ctx.get("offset", 0))
                limit  = ctx.get("limit")
                limit  = int(limit) if limit is not None else None
                data   = await _exec.execute_read(path, offset=offset, limit=limit)

                notice = None
                if data["has_more"]:
                    notice = SuppressedContentNotice.file_too_large(
                        tool="tr_read_file",
                        path=str(path),
                        original_size_chars=len(data["content"]),
                        total_lines=data["total_lines"],
                        shown_lines=data["offset"] + data["returned_lines"],
                    )

                return ReadResult(
                    content=data["content"],
                    offset=data["offset"],
                    limit=limit,
                    returned_lines=data["returned_lines"],
                    total_lines=data["total_lines"],
                    has_more=data["has_more"],
                    truncation_notice=notice,
                )

            case OperationType.WRITE | OperationType.CREATE:
                content = str(ctx.get("content", ""))
                await _exec.execute_write(path, content)
                return WriteResult(written_to=str(path))

            case OperationType.DELETE:
                await _exec.execute_delete(path)
                return DeleteResult(deleted=str(path))

            case OperationType.EXECUTE:
                command = str(ctx.get("command", ""))
                _timeout = ctx.get("timeout", 120)
                timeout = int(_timeout) if isinstance(_timeout, (int, float, str)) else 120
                _cwd = ctx.get("cwd")
                cwd = Path(str(_cwd)) if _cwd is not None else self.sandbox_root
                raw = await _exec.execute_run(command, cwd, timeout)
                return ShellResult(
                    stdout=raw["stdout"],
                    stderr=raw["stderr"],
                    exit_code=raw["exit_code"],
                )

            case _:
                raise NotImplementedError(
                    f"Operation '{request.operation.value}' execution not implemented."
                )

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _denied(
        self,
        request: OperationRequest,
        operation_id: str,
        *,
        error: str,
        error_code: str,
        zone: FilesystemZone | None,
        alternatives: list[str] | None = None,
    ) -> OperationResponse:
        return OperationResponse(
            request_id=request.request_id,
            operation_id=operation_id,
            status="denied",
            error=error,
            error_code=error_code,
            alternatives=alternatives or [],
            zone=zone.value if zone else None,
            operation=request.operation.value,
        )

    def _log_denied(
        self,
        request: OperationRequest,
        zone: FilesystemZone,
        action: PermissionAction,
        error: str,
        user_decision: str | None = None,
    ) -> None:
        logger.warning(
            "TaskRunner | DENIED | agent=%s | op=%s | zone=%s | path=%s | reason=%s%s",
            request.requesting_agent,
            request.operation.value.upper(),
            zone.value,
            request.paths[0] if request.paths else "<none>",
            error,
            f" | user_decision={user_decision}" if user_decision else "",
        )
