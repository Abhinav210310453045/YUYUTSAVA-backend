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
from yuyutsava.consent.models import parse_consent_decision as _parse_consent_decision
from yuyutsava.models.operations import (
    FilesystemZone,
    OperationRequest,
    OperationResponse,
    OperationType,
    PermissionAction,
)
from yuyutsava.models.results import (
    DeleteResult,
    DirEntry,
    ListResult,
    ReadResult,
    ShellResult,
    WriteResult,
)
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
        sandbox_root: Path | None = None,
        policy: object | None = None,  # PermissionsPolicy; untyped to avoid daemon-side import
        consent: object | None = None,  # consent.ConsentRegistry; duck-typed (allowlist)
    ) -> None:
        self.workspace_root: Path = workspace_root.resolve()
        self.sandbox_root: Path = (
            sandbox_root.resolve() if sandbox_root is not None
            else (self.workspace_root / "_sandbox").resolve()
        )
        self._policy = policy
        self._consent = consent

    @staticmethod
    def _policy_tool_name(op: OperationType) -> str:
        """Map an operation type to the conventional ``tr_*`` tool name.

        Used to look the request up in the permission policy file. The mapping
        mirrors the names defined in :mod:`yuyutsava.agents.task_runner.tools`
        — keep them in sync.
        """
        return {
            OperationType.READ:    "tr_read_file",
            OperationType.WRITE:   "tr_write_file",
            OperationType.CREATE:  "tr_write_file",
            OperationType.DELETE:  "tr_delete_file",
            OperationType.EXECUTE: "tr_execute",
            OperationType.CHMOD:   "tr_chmod",
            OperationType.LIST:    "tr_ls",
            OperationType.GLOB:    "tr_glob",
        }.get(op, "tr_unknown")

    # ------------------------------------------------------------------
    # Consent (allowlist) helpers
    # ------------------------------------------------------------------

    def _consent_verdict(self, request: OperationRequest, zone: FilesystemZone) -> str:
        """Return 'allow' / 'deny' / 'prompt' from the consent registry."""
        from yuyutsava.core.agent_context import current_context

        session_id = current_context().get("session_id")
        try:
            return self._consent.check_tool_permission(  # type: ignore[attr-defined]
                operation=request.operation.value, zone=zone.value,
                paths=request.paths, session_id=session_id,
                workspace=str(self.workspace_root),
            )
        except Exception:
            logger.exception("consent check failed; falling through to prompt")
            return "prompt"

    async def _record_consent_grant(
        self, request: OperationRequest, zone: FilesystemZone, scope: str
    ) -> None:
        """Persist/remember an 'allow for <scope>' grant for this op+zone.

        For in-workspace operations the grant is widened to the **workspace root**
        so one approval covers the operation type everywhere under the workspace
        (no per-subfolder re-asks). External paths (e.g. the ``/host`` sentinel
        used by bash/``tr_execute``) keep their auto-derived directory.
        """
        from yuyutsava.core.agent_context import current_context

        session_id = current_context().get("session_id")
        directory = str(self.workspace_root) if zone == FilesystemZone.WORKSPACE else None
        try:
            await self._consent.grant_tool_permission(  # type: ignore[attr-defined]
                operation=request.operation.value, zone=zone.value,
                paths=request.paths, scope=scope, session_id=session_id,
                workspace=str(self.workspace_root), directory=directory,
            )
        except Exception:
            logger.exception("consent grant failed (continuing)")

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
            # Tier-1.5 policy override: a matching ``auto_approve`` entry in
            # ~/.yuyutsava/permissions.json bypasses the prompt entirely.
            if self._policy is not None:
                tool_name = self._policy_tool_name(request.operation)
                if self._policy.policy_for(tool_name) == "auto_approve":  # type: ignore[attr-defined]
                    logger.info(
                        "TaskRunner | POLICY auto_approve | %s | %s",
                        request.operation.value.upper(), primary_path,
                    )
                    action = PermissionAction.ALLOW  # fall through to execute
            # Allowlist (consent) check: a prior "allow for session/project" grant
            # covering this op+zone+directory skips the prompt entirely. This is
            # what stops the per-file re-approval storm.
            if action == PermissionAction.PROMPT and self._consent is not None:
                verdict = self._consent_verdict(request, zone)
                if verdict == "allow":
                    logger.info(
                        "TaskRunner | CONSENT allow (grant) | %s | %s",
                        request.operation.value.upper(), primary_path,
                    )
                    action = PermissionAction.ALLOW
                elif verdict == "deny":
                    error_msg = (
                        f"A standing rule denies {request.operation.value.upper()} "
                        f"on '{primary_path}'."
                    )
                    self._log_denied(request, zone, action, error_msg, user_decision="reject")
                    return self._denied(
                        request, operation_id, error=error_msg,
                        error_code=_EC_USER_DENIED, zone=zone,
                        alternatives=get_alternatives(zone, request.operation),
                    )
            if action == PermissionAction.PROMPT:
                payload = build_interrupt_payload(request, zone)
                decision: str = interrupt(payload.to_interrupt_dict())

                # The resume token carries both the verdict and the scope the user
                # chose (once / session / project). Record a grant when asked.
                allow, scope = _parse_consent_decision(decision)
                if not allow:
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
                if scope is not None and self._consent is not None:
                    await self._record_consent_grant(request, zone, scope)

                logger.info(
                    "TaskRunner | APPROVED by user (%s) | %s | %s",
                    scope or "once", request.operation.value.upper(), primary_path,
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
    ) -> ShellResult | WriteResult | DeleteResult | ReadResult | ListResult:
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

            case OperationType.LIST:
                _max = ctx.get("max_entries", 500)
                max_entries = int(_max) if isinstance(_max, (int, float, str)) else 500
                data = await _exec.execute_list(path, max_entries=max_entries)
                entries = [DirEntry(**e) for e in data["entries"]]
                return ListResult(
                    root=str(path),
                    pattern=None,
                    entries=entries,
                    returned=len(entries),
                    total=data["total"],
                    has_more=data["has_more"],
                )

            case OperationType.GLOB:
                pattern = str(ctx.get("pattern", "*"))
                _max = ctx.get("max_entries", 500)
                max_entries = int(_max) if isinstance(_max, (int, float, str)) else 500
                data = await _exec.execute_glob(path, pattern, max_entries=max_entries)
                entries = [DirEntry(**e) for e in data["entries"]]
                return ListResult(
                    root=str(path),
                    pattern=pattern,
                    entries=entries,
                    returned=len(entries),
                    total=data["total"],
                    has_more=data["has_more"],
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
