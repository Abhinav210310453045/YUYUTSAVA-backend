"""Typed records returned from storage reads.

Every store's read-side method returns one of these (or ``None`` / ``list[T]``),
never ``dict[str, Any]``. Writes accept the same models. This kills the
half-typed pattern where the previous code wrote ``Proposal`` dataclasses but
read back plain dicts.

Frozen dataclasses are used throughout — store rows are values, not entities,
so mutating them in callers is a smell. Construct a new instance with the
edit applied, or call back into the store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID


# ---------------------------------------------------------------------------
# Sessions (sessions.db)
# ---------------------------------------------------------------------------


SESSION_STATUSES = ("running", "idle", "crashed", "done")


@dataclass(frozen=True)
class Session:
    """One persisted CLI session — the row the store hands back to callers."""

    id: str
    thread_id: str
    workspace: Path
    status: str
    created_at: float
    updated_at: float
    message_count: int
    memory_files_count: int
    db_row_bytes: int
    task_preview: str
    schema_version: int = 1
    # Which human↔agent interface produced this session: "cli" (terminal /
    # Electron text chat) or "voice" (the voice agent). The Sessions UI splits
    # on this column, so the distinction is DB-backed, not a UI artifact.
    origin: str = "cli"


# ---------------------------------------------------------------------------
# Events (state.db — event_payloads, proposals, decisions, consent_rules)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventRecord:
    """One row from ``event_payloads`` returned to callers.

    Replaces the ``dict[str, Any]`` previously returned by
    ``Store.get_event_payload`` — the payload itself stays a dict because it's
    free-form per-source JSON, but the envelope is typed.
    """

    event_id: str
    topic: str
    ts: float
    payload: dict
    blob_path: str | None


@dataclass(frozen=True)
class Proposal:
    """Tier-1 consent record shown to the user *before* any orchestrator LLM call."""

    proposal_id: str
    event_id: str
    topic: str
    summary: str
    proposed: str
    subagent: str
    urgency: int
    created_ts: float
    expires_ts: float
    status: str = "pending"
    session_id: str | None = None
    agent_path: str | None = None

    @classmethod
    def new(
        cls,
        *,
        event_id: str,
        topic: str,
        summary: str,
        proposed: str,
        subagent: str,
        urgency: int,
        expiry_sec: int,
        session_id: str | None = None,
        agent_path: str | None = None,
    ) -> Proposal:
        now = time.time()
        return cls(
            proposal_id=str(ULID()),
            event_id=event_id,
            topic=topic,
            summary=summary,
            proposed=proposed,
            subagent=subagent,
            urgency=urgency,
            created_ts=now,
            expires_ts=now + expiry_sec,
            status="pending",
            session_id=session_id,
            agent_path=agent_path,
        )


@dataclass(frozen=True)
class Decision:
    """One row from ``decisions``. Replaces dict returned by list_decisions/recall."""

    decision_id: str
    proposal_id: str | None
    event_id: str
    outcome: str
    action_summary: str | None
    ts: float
    session_id: str | None = None
    agent_path: str | None = None


@dataclass(frozen=True)
class ConsentRule:
    """Auto-approve / auto-skip rule matched against incoming events."""

    rule_id: str
    topic_glob: str
    match_json: str
    decision: str               # "auto_approve" | "auto_skip"
    created_ts: float
    expires_ts: float | None


# ---------------------------------------------------------------------------
# Preferences (state.db::user_prefs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pref:
    """One stored user preference. ``value`` is whatever JSON-serialisable type
    the caller stored — kept loose because pref values are intentionally
    schema-free per-key."""

    key: str
    value: object
    updated_ts: float


# ---------------------------------------------------------------------------
# Interrupts (interrupts.db)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterruptRecord:
    """HITL audit-log entry. Replaces the loose ``dict`` previously passed to
    ``InterruptsStore.record`` and the loose ``dict`` returned by its reads.

    ``payload`` stays a free-form dict so future interrupt kinds don't require
    schema changes; the structured columns (``kind``, ``operation``, ``zone``,
    ``risk_level``, …) are projected out for query-ability.

    Audit fields (``id``, ``outcome``, ``user_response``, ``created_at``,
    ``resolved_at``) are ``None`` on a record being written and populated when
    the store hands one back from a read.
    """

    session_id: str
    thread_id: str
    invocation_mode: str           # "cli" | "daemon"
    payload: dict                  # original interrupt value
    # Derived / structured fields — populated from payload at construction.
    kind: str = "other"
    agent_path: str = "unknown"
    requesting_agent: str | None = None
    parent_agent: str | None = None
    operation: str | None = None
    paths: list[str] | None = None
    zone: str | None = None
    risk_level: str | None = None
    reason: str | None = None
    question: str | None = None
    # Audit fields — populated by reads, None on writes.
    id: str | None = None
    outcome: str | None = None
    user_response: str | None = None
    created_at: float | None = None
    resolved_at: float | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict,
        *,
        session_id: str,
        thread_id: str,
        invocation_mode: str,
    ) -> InterruptRecord:
        """Build a record from a raw interrupt payload dict.

        Extracts the structured fields (``kind``, ``agent_path``,
        ``operation``, …) up front so the store's INSERT can rely on them
        being typed strings rather than ``.get()``-ing out of a loose dict.
        """
        if not isinstance(payload, dict):
            payload = {}
        paths_raw = payload.get("paths")
        paths = list(paths_raw) if isinstance(paths_raw, (list, tuple)) else None
        return cls(
            session_id=session_id,
            thread_id=thread_id,
            invocation_mode=invocation_mode,
            payload=payload,
            kind=str(payload.get("type") or "other"),
            agent_path=str(payload.get("agent_path") or invocation_mode or "unknown"),
            requesting_agent=payload.get("requesting_agent"),
            parent_agent=payload.get("parent_agent"),
            operation=payload.get("operation"),
            paths=paths,
            zone=payload.get("zone"),
            risk_level=payload.get("risk_level"),
            reason=payload.get("reason"),
            question=payload.get("question"),
        )
