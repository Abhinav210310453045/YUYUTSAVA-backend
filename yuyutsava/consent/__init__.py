"""Reusable consent / allowlist core.

One small engine that answers ALLOW / DENY / PROMPT for any *(domain, subject,
scope)* and records **Grants** at a chosen scope (once / session / project /
persistent). Domains plug in a way to build a subject key and to match a stored
grant against a request — the scope/expiry/precedence logic is shared, so events,
proposals, tasks, and tool-permissions all reuse the same code instead of forking
their own allowlist.

Wired as a DI singleton (one ``ConsentRegistry`` per daemon / per CLI process).

Public surface::

    from yuyutsava.consent import (
        ConsentRegistry, ConsentScope, Verdict,
        ToolPermissionDomain, parse_consent_decision, is_permission_ask,
    )
"""

from __future__ import annotations

from yuyutsava.consent.domains import ConsentDomain, ToolPermissionDomain
from yuyutsava.consent.models import (
    ConsentDecision,
    ConsentRequest,
    ConsentScope,
    Grant,
    Verdict,
    decision_token,
    is_permission_ask,
    parse_consent_decision,
)
from yuyutsava.consent.registry import ConsentRegistry
from yuyutsava.consent.store import ConsentStore

__all__ = [
    "ConsentRegistry",
    "ConsentStore",
    "ConsentDomain",
    "ToolPermissionDomain",
    "ConsentScope",
    "Verdict",
    "Grant",
    "ConsentRequest",
    "ConsentDecision",
    "parse_consent_decision",
    "decision_token",
    "is_permission_ask",
]
