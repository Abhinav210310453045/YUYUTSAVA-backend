"""ConsentRegistry — the reusable allowlist engine.

``check(request)`` returns ALLOW / DENY / PROMPT by matching active grants in the
scopes relevant to the request; ``grant(...)`` records a decision at a chosen
scope. An in-memory cache of every active grant (session + persisted) makes a
just-granted decision visible to the very next ``check`` regardless of the
store's write latency; PROJECT / PERSISTENT grants are additionally persisted.

DI singleton: build one per daemon (or per CLI process) and share it — via
``set_default_consent`` for the TaskRunner tool registry and (later) via the
triage loop for the event domain.
"""

from __future__ import annotations

import logging
import time
import uuid

from yuyutsava.consent.domains import ConsentDomain, ToolPermissionDomain
from yuyutsava.consent.models import (
    ConsentDecision,
    ConsentRequest,
    ConsentScope,
    Grant,
    Verdict,
)
from yuyutsava.consent.store import ConsentStore

logger = logging.getLogger("yuyutsava.consent")


def _tool_scope_refs(session_id: str | None, workspace: str | None) -> dict[str, str]:
    refs: dict[str, str] = {ConsentScope.PERSISTENT.value: "*"}
    if session_id:
        refs[ConsentScope.SESSION.value] = session_id
    if workspace:
        refs[ConsentScope.PROJECT.value] = str(workspace)
    return refs


def _ref_for_scope(scope: str, scope_refs: dict[str, str]) -> str:
    if scope == ConsentScope.PERSISTENT.value:
        return "*"
    return scope_refs.get(scope) or "*"


class ConsentRegistry:
    def __init__(
        self,
        *,
        store: ConsentStore | None = None,
        domains: list[ConsentDomain] | None = None,
    ) -> None:
        self._store = store
        self._domains: dict[str, ConsentDomain] = {
            d.name: d for d in (domains or [ToolPermissionDomain()])
        }
        # In-memory cache of every active grant (session + persisted). Loaded
        # from the store at boot so persisted PROJECT grants survive restarts.
        self._grants: list[Grant] = []
        if store is not None:
            try:
                self._grants.extend(store.list_consent_grants())
            except Exception:
                logger.exception("consent: loading persisted grants failed")

    def register_domain(self, domain: ConsentDomain) -> None:
        self._domains[domain.name] = domain

    # ------------------------------------------------------------------
    # Generic engine
    # ------------------------------------------------------------------

    def check(self, request: ConsentRequest) -> ConsentDecision:
        domain = self._domains.get(request.domain)
        if domain is None:
            return ConsentDecision(verdict=Verdict.PROMPT.value)
        now = time.time()
        refs = set(request.scope_refs.values())
        for g in self._grants:
            if g.domain != request.domain or not g.is_active(now):
                continue
            # Persistent grants ("*") always apply; scoped grants only when their
            # ref is relevant to this request (its session / its workspace).
            if g.scope_ref != "*" and g.scope_ref not in refs:
                continue
            if domain.matches(g, request):
                return ConsentDecision(verdict=g.decision, grant=g)
        return ConsentDecision(verdict=Verdict.PROMPT.value)

    async def grant(
        self,
        *,
        domain: str,
        descriptor: dict,
        decision: str,
        scope: str,
        scope_refs: dict[str, str],
        expires_ts: float | None = None,
    ) -> Grant:
        dom = self._domains[domain]
        g = Grant(
            grant_id=str(uuid.uuid4()),
            domain=domain,
            subject_key=dom.subject_key(descriptor),
            decision=decision,
            scope=scope,
            scope_ref=_ref_for_scope(scope, scope_refs),
            created_ts=time.time(),
            expires_ts=expires_ts,
        )
        self._grants.append(g)
        if scope in (ConsentScope.PROJECT.value, ConsentScope.PERSISTENT.value) and self._store is not None:
            try:
                await self._store.put_consent_grant(g)
            except Exception:
                logger.exception("consent: persisting grant failed")
        return g

    # ------------------------------------------------------------------
    # Tool-permission convenience (keeps the TaskRunner gateway decoupled:
    # it passes primitives and gets back a verdict string / records a grant).
    # ------------------------------------------------------------------

    def check_tool_permission(
        self,
        *,
        operation: str,
        zone: str,
        paths: list[str],
        session_id: str | None,
        workspace: str | None,
    ) -> str:
        return self.check(ConsentRequest(
            domain=ToolPermissionDomain.name,
            scope_refs=_tool_scope_refs(session_id, workspace),
            descriptor={"operation": operation, "zone": zone, "paths": list(paths or [])},
        )).verdict

    async def grant_tool_permission(
        self,
        *,
        operation: str,
        zone: str,
        paths: list[str],
        scope: str,
        session_id: str | None,
        workspace: str | None,
        decision: str = Verdict.ALLOW.value,
        directory: str | None = None,
    ) -> Grant:
        """Record an allow/deny grant for an op+zone.

        ``directory`` overrides the auto-derived grant directory (normally the
        common parent of ``paths``). Callers pass the workspace root here so a
        session/project grant covers the operation for the *whole* workspace —
        one approval, no per-subfolder re-asks.
        """
        descriptor: dict = {"operation": operation, "zone": zone, "paths": list(paths or [])}
        if directory:
            descriptor["directory"] = directory
        return await self.grant(
            domain=ToolPermissionDomain.name,
            descriptor=descriptor,
            decision=decision,
            scope=scope,
            scope_refs=_tool_scope_refs(session_id, workspace),
        )
