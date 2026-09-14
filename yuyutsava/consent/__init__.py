"""Consent: one allowlist engine for every kind of permission question.

It answers ALLOW, DENY or PROMPT for a *(domain, subject, scope)* and records
grants at a chosen scope (once, session, project or persistent). A domain supplies
how to build a subject key and how a stored grant matches a request; scope, expiry
and precedence are shared, so events, proposals, tasks and tool permissions reuse
one engine instead of each keeping its own allowlist. One
:class:`~yuyutsava.consent.registry.ConsentRegistry` is created per daemon or CLI
process.

The names below are re-exported because other modules import them from here;
import anything else from the module that owns it.
"""

from __future__ import annotations

from yuyutsava.consent.models import (
    ConsentScope,
    Grant,
    Verdict,
    decision_token,
    is_permission_ask,
    parse_consent_decision,
)
from yuyutsava.consent.registry import ConsentRegistry

__all__ = [
    "ConsentRegistry",
    "ConsentScope",
    "Grant",
    "Verdict",
    "decision_token",
    "is_permission_ask",
    "parse_consent_decision",
]
