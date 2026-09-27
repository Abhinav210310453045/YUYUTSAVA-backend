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

from yuyutsava.agents.task_runner.dispatch import run_operation
from yuyutsava.agents.task_runner.exec_backend import ExecBackend, HostExecBackend
from yuyutsava.agents.task_runner.permissions import (
    build_interrupt_payload,
    decide,
    get_alternatives,
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
from yuyutsava.run_context import current_context
from yuyutsava.storage.paths import WorkspaceLayout

logger = logging.getLogger("yuyutsava.task_runner")

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

_EC_PATH_TRAVERSAL = "TR001"
_EC_SYSTEM_CRITICAL = "TR002"
_EC_USER_DENIED = "TR003"
_EC_RULE_DENIED = "TR004"
_EC_EXEC_ERROR = "TR005"


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
        exec_backend: ExecBackend | None = None,
    ) -> None:
        self.workspace_root: Path = workspace_root.resolve()
        # Default sandbox = <ws>/.yuyutsava/sandbox — from the one layout helper.
        self.sandbox_root: Path = (
            sandbox_root.resolve()
            if sandbox_root is not None
            else WorkspaceLayout.for_workspace(self.workspace_root).sandbox
        )
        self._policy = policy
        self._consent = consent
        # WHERE sandbox-cwd commands/scripts run once allowed: the host by
        # default, the Docker container when the docker bundle installs its
        # backend (see exec_backend.py). Zones/permissions are unaffected.
        self.exec_backend: ExecBackend = exec_backend or HostExecBackend()

    @staticmethod
    def _policy_tool_name(op: OperationType) -> str:
        """Map an operation type to the conventional ``tr_*`` tool name.

        Used to look the request up in the permission policy file. The mapping
        mirrors the names defined in :mod:`yuyutsava.agents.task_runner.tools`
        — keep them in sync.
        """
        return {
            OperationType.READ: "tr_read_file",
            OperationType.WRITE: "tr_write_file",
            OperationType.CREATE: "tr_write_file",
            OperationType.DELETE: "tr_delete_file",
            OperationType.EXECUTE: "tr_execute",
            OperationType.CHMOD: "tr_chmod",
            OperationType.LIST: "tr_ls",
            OperationType.GLOB: "tr_glob",
        }.get(op, "tr_unknown")

    # ------------------------------------------------------------------
    # Consent (allowlist) helpers
    # ------------------------------------------------------------------

    def _consent_verdict(self, request: OperationRequest, zone: FilesystemZone) -> str:
        """Return 'allow' / 'deny' / 'prompt' from the consent registry."""

        session_id = current_context().get("session_id")
        try:
            return self._consent.check_tool_permission(  # type: ignore[attr-defined]
                operation=request.operation.value,
                zone=zone.value,
                paths=request.paths,
                session_id=session_id,
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

        session_id = current_context().get("session_id")
        directory = str(self.workspace_root) if zone == FilesystemZone.WORKSPACE else None
        try:
            await self._consent.grant_tool_permission(  # type: ignore[attr-defined]
                operation=request.operation.value,
                zone=zone.value,
                paths=request.paths,
                scope=scope,
                session_id=session_id,
                workspace=str(self.workspace_root),
                directory=directory,
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
                request,
                operation_id,
                error="No paths specified in request.",
                error_code=_EC_RULE_DENIED,
                zone=None,
            )
        primary_path = request.paths[0]

        # 1. Classify zone (canonicalizes internally), 2. apply the rule table.
        zone = classify_zone(primary_path, self.workspace_root, self.sandbox_root)
        action = decide(zone, request.operation)
        logger.debug(
            "TaskRunner | %s | zone=%s | action=%s | path=%s",
            request.operation.value.upper(),
            zone.value,
            action.value,
            primary_path,
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
            return self._deny(request, operation_id, zone, action, error_msg, code)

        # 4. PROMPT — policy, a standing grant, or the user decides
        if action == PermissionAction.PROMPT:
            refusal = await self._resolve_prompt(request, operation_id, zone)
            if refusal is not None:
                return refusal

        # 5. ALLOW (or approved) — execute
        return await self._run(request, operation_id, zone)

    def _deny(
        self,
        request: OperationRequest,
        operation_id: str,
        zone: FilesystemZone,
        action: PermissionAction,
        error_msg: str,
        code: str,
        *,
        user_decision: str | None = None,
    ) -> OperationResponse:
        """Log the refusal, and answer with the zone's alternatives."""
        self._log_denied(request, zone, action, error_msg, user_decision=user_decision)
        return self._denied(
            request,
            operation_id,
            error=error_msg,
            error_code=code,
            zone=zone,
            alternatives=get_alternatives(zone, request.operation),
        )

    async def _resolve_prompt(
        self, request: OperationRequest, operation_id: str, zone: FilesystemZone
    ) -> OperationResponse | None:
        """Decide a PROMPT operation: a refusal to return, or ``None`` to run it.

        In order: the policy file's ``auto_approve``, then a standing consent
        grant (allow or deny), then the user via LangGraph ``interrupt()``. An
        elevated (admin/root) request is CRITICAL: neither the policy nor a
        cached grant can satisfy it — it always asks fresh — and approving it
        never records a grant.
        """
        primary_path = request.paths[0]
        op = request.operation.value.upper()
        elevated = bool((request.additional_context or {}).get("elevated"))

        # Tier-1.5 policy override: a matching ``auto_approve`` entry in
        # ~/.yuyutsava/permissions.json bypasses the prompt entirely.
        if self._policy is not None and not elevated:
            tool_name = self._policy_tool_name(request.operation)
            if self._policy.policy_for(tool_name) == "auto_approve":  # type: ignore[attr-defined]
                logger.info("TaskRunner | POLICY auto_approve | %s | %s", op, primary_path)
                return None

        # Allowlist (consent) check: a prior "allow for session/project" grant
        # covering this op+zone+directory skips the prompt entirely. This is
        # what stops the per-file re-approval storm.
        if self._consent is not None and not elevated:
            verdict = self._consent_verdict(request, zone)
            if verdict == "allow":
                logger.info("TaskRunner | CONSENT allow (grant) | %s | %s", op, primary_path)
                return None
            if verdict == "deny":
                return self._deny(
                    request,
                    operation_id,
                    zone,
                    PermissionAction.PROMPT,
                    f"A standing rule denies {op} on '{primary_path}'.",
                    _EC_USER_DENIED,
                    user_decision="reject",
                )

        payload = build_interrupt_payload(request, zone)
        decision: str = interrupt(payload.to_interrupt_dict())
        # The resume token carries both the verdict and the scope the user
        # chose (once / session / project). Record a grant when asked.
        allow, scope = _parse_consent_decision(decision)
        if not allow:
            return self._deny(
                request,
                operation_id,
                zone,
                PermissionAction.PROMPT,
                f"User denied permission for {op} on '{primary_path}'.",
                _EC_USER_DENIED,
                user_decision="reject",
            )
        if scope is not None and self._consent is not None and not elevated:
            await self._record_consent_grant(request, zone, scope)
        logger.info(
            "TaskRunner | APPROVED by user (%s) | %s | %s", scope or "once", op, primary_path
        )
        return None

    @staticmethod
    def _exec_error(
        request: OperationRequest, operation_id: str, zone: FilesystemZone, error_msg: str
    ) -> OperationResponse:
        return OperationResponse(
            request_id=request.request_id,
            operation_id=operation_id,
            status="error",
            error=error_msg,
            error_code=_EC_EXEC_ERROR,
            zone=zone.value if zone else None,
            operation=request.operation.value,
        )

    async def _run(
        self, request: OperationRequest, operation_id: str, zone: FilesystemZone
    ) -> OperationResponse:
        """Execute an allowed operation; an executor exception is an error response."""
        try:
            result = await run_operation(
                request, exec_backend=self.exec_backend, sandbox_root=self.sandbox_root
            )
        except TimeoutError:
            # The executor's documented outcome for a command or script that
            # overran its limit: expected, so one line — and a message the agent
            # can act on, where str(TimeoutError()) is empty.
            error_msg = "TimeoutError: did not finish within its time limit and was stopped"
            logger.warning("TaskRunner | EXEC TIMEOUT | %s", request.operation.value)
            return self._exec_error(request, operation_id, zone, error_msg)
        except Exception as exc:  # noqa: BLE001 — returned to the agent as an error
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("TaskRunner | EXEC ERROR | %s", error_msg, exc_info=True)
            return self._exec_error(request, operation_id, zone, error_msg)

        logger.info(
            "TaskRunner | SUCCESS | %s | zone=%s | path=%s",
            request.operation.value.upper(),
            zone.value if zone else "?",
            request.paths[0],
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
