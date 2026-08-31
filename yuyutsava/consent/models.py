"""Dependency-free consent value types + decision parsing.

Kept import-light (stdlib only) so any layer — the TaskRunner gateway, the
storage Store, the daemon channels, the CLI — can use these without pulling in
the rest of the stack.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ConsentScope(str, Enum):
    """How long / how widely a remembered decision applies."""

    ONCE = "once"             # not remembered — this request only
    SESSION = "session"       # remembered for the current session/thread (in-memory)
    PROJECT = "project"       # remembered for this workspace (persisted)
    PERSISTENT = "persistent" # remembered everywhere (persisted)


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


@dataclass(frozen=True)
class Grant:
    """A remembered consent decision for a subject within a scope."""

    grant_id: str
    domain: str             # "tool_permission" | "event" | …
    subject_key: str        # domain-built normalized key (what is being allowed)
    decision: str           # Verdict.ALLOW / Verdict.DENY value
    scope: str              # ConsentScope value
    scope_ref: str          # session_id / workspace root / "*" (persistent)
    created_ts: float
    expires_ts: float | None = None

    def is_active(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.expires_ts is None or self.expires_ts > now


@dataclass(frozen=True)
class ConsentRequest:
    """One check against the registry.

    ``descriptor`` carries the raw, domain-specific fields the domain needs to
    match against stored grants (e.g. operation/zone/paths for tool permissions).
    ``scope_refs`` maps each scope to its concrete ref for this request, e.g.
    ``{"session": thread_id, "project": workspace, "persistent": "*"}``.
    """

    domain: str
    scope_refs: dict[str, str]
    descriptor: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConsentDecision:
    verdict: str                 # Verdict value
    grant: Grant | None = None   # the grant that decided it (None for PROMPT)


# ---------------------------------------------------------------------------
# Decision-word parsing (shared by every surface: CLI words, Electron buttons,
# resume tokens). Maps the vocabulary a user/UI can produce to (allow, scope).
# ---------------------------------------------------------------------------

_ALLOW_ONCE = frozenset({"approve", "approve_once", "once", "a", "y", "yes", "ok", "allow"})
_ALLOW_SESSION = frozenset({"session", "approve_session", "s"})
_ALLOW_PROJECT = frozenset({"project", "approve_project", "p"})
# (kept for symmetry / explicitness; anything not allow-* falls through to reject)
_REJECT = frozenset({"reject", "no", "n", "r", "deny", "cancel"})


def parse_consent_decision(answer: str | None) -> tuple[bool, str | None]:
    """Map a response token to ``(allow, scope)``.

    ``scope`` is a :class:`ConsentScope` value when the user chose to remember,
    else ``None`` (allow once). Non-affirmative tokens → ``(False, None)``.
    """
    a = (answer or "").strip().lower()
    if a in _ALLOW_SESSION:
        return True, ConsentScope.SESSION.value
    if a in _ALLOW_PROJECT:
        return True, ConsentScope.PROJECT.value
    if a in _ALLOW_ONCE:
        return True, None
    return False, None


def decision_token(answer: str | None) -> str | None:
    """Map a bare word to the response token to send, or ``None`` if not a
    decision word (so a normal chat message isn't swallowed as an approval).

    Returns one of ``approve`` / ``approve_session`` / ``approve_project`` /
    ``reject`` — the tokens the TaskRunner's resume parser understands.
    """
    a = (answer or "").strip().lower()
    if a in _ALLOW_SESSION:
        return "approve_session"
    if a in _ALLOW_PROJECT:
        return "approve_project"
    if a in _ALLOW_ONCE:
        return "approve"
    if a in _REJECT:
        return "reject"
    return None


def is_permission_ask(options: list[str] | None) -> bool:
    """True when an ask's options are an approve/reject permission prompt.

    A subset check (not equality) so scope options (session/project) can be added
    without breaking permission detection across the CLI + UI surfaces.
    """
    opts = set(options or [])
    return "approve" in opts and "reject" in opts
