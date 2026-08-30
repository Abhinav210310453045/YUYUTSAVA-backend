"""Narrow roles over the events ``Store`` facade — the ISP half of ``F-S03``.

``Store`` exposes ~30 methods spanning eight unrelated concerns: event payloads,
proposals, decisions, pending asks, consent rules, consent grants, tool counters
and preferences. Twenty-two modules declare a dependency on it.

**Measured usage, 2026-08-08:**

===============================================  ==================
Consumer                                         Store methods used
===============================================  ==================
``storage/prefs.py``                             4 of 30
``daemon/task_submission.py``                    3
``daemon/ask_registry.py``                       3
``daemon/triage_loop.py``                        3
``daemon/consent.py``                            1
``core/policy.py``                               1
``agents/orchestrator/spawn.py``                 1
``daemon/orchestrator_loop.py``                  1
…18 more                                         1–2 each
===============================================  ==================

The median consumer needs **one or two** methods. Nobody needs the god object,
and ``store: Store`` in a signature tells a reader nothing about what the
function touches — they have to read the body.

These Protocols name what each consumer actually needs. They are structural, so
``Store`` satisfies every one of them without inheriting anything and without a
single call site changing: only the *declared* type moves. That is deliberate —
the review's direction for ``F-S03`` was to narrow signatures, not to restructure
the facade, because the facade is a legitimate construction-time aggregate.

What this buys, concretely:

* a signature states its blast radius — ``store: DecisionWriter`` says "writes
  decisions", where ``store: Store`` said "could touch anything";
* a test double satisfies 1–3 methods instead of 30;
* a change to consent-grant storage no longer has 22 modules in its
  type-level blast radius when only two use it.

Grouping follows *observed* usage rather than invented taxonomy: every Protocol
here exists because at least one real consumer needs exactly that set.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DecisionWriter(Protocol):
    """Record an outcome on the timeline.

    The single most common dependency: ``triage_loop``, ``orchestrator_loop``,
    ``task_submission`` and ``orchestrator/spawn`` each want only this.
    """

    async def put_decision(
        self, *, proposal_id: str | None, event_id: str, outcome: str,
        action_summary: str | None = ..., ts: float | None = ...,
        session_id: str | None = ..., agent_path: str | None = ...,
    ) -> None: ...


@runtime_checkable
class DecisionReader(Protocol):
    """Read the decision timeline (``GET /decisions``)."""

    async def list_decisions(self, limit: int = ..., cursor: float | None = ...) -> list[Any]: ...


@runtime_checkable
class ProposalWriter(Protocol):
    async def put_proposal(self, p: Any) -> None: ...
    async def try_set_proposal_status(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class EventPayloadWriter(Protocol):
    """Used by event sources when publishing."""

    async def put_event_payload(
        self, *, event_id: str, topic: str, ts: float,
        payload: dict[str, Any], blob_path: str | None = ...,
    ) -> None: ...


@runtime_checkable
class EventPayloadReader(Protocol):
    """Used by ``ev_fetch_event``: read one payload back, nothing else.

    The reader half of :class:`EventPayloadWriter`. Added in Phase 2 step 2.7
    when the tool that needed it was narrowed — the pair had a writer and a
    sweeper but no reader, so the only available annotation was the whole Store.
    """

    async def get_event_payload(self, event_id: str) -> Any: ...


@runtime_checkable
class EventPayloadSweeper(Protocol):
    """The TTL sweeper's whole need — two deletes out of thirty methods."""

    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int: ...
    async def delete_event_payloads_with_blob_prefix(
        self, prefix: str, older_than_ts: float
    ) -> int: ...


@runtime_checkable
class RecallReader(Protocol):
    """Topic-scoped recall for the ``ev_recall`` / ``ctx_*`` tools."""

    async def recall(
        self, topic_glob: str, since_sec: float, limit: int = ...
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class ConsentRuleReader(Protocol):
    """``ConsentEvaluator`` and ``GET /rules`` only read."""

    async def list_consent_rules(self) -> list[Any]: ...


@runtime_checkable
class ConsentRuleWriter(Protocol):
    async def put_consent_rule(self, rule: Any) -> None: ...


@runtime_checkable
class PendingAskRegistry(Protocol):
    """The ask lifecycle, and nothing else."""

    async def put_pending_ask(self, record: dict[str, Any]) -> None: ...
    async def resolve_pending_ask(self, *args: Any, **kwargs: Any) -> Any: ...
    async def list_pending_asks(self, limit: int = ...) -> list[dict[str, Any]]: ...


@runtime_checkable
class ToolCallCounter(Protocol):
    """``StorePolicyCapEnforcer`` rate-caps ws_* searches with these two."""

    async def incr_tool_call(self, tool_name: str, day: str) -> int: ...
    async def get_tool_call_count(self, tool_name: str, day: str) -> int: ...


@runtime_checkable
class PrefsBackend(Protocol):
    """What ``PrefsStore`` wraps — the widest real consumer, at 4 methods."""

    async def put_pref(self, key: str, value: Any) -> None: ...
    async def delete_pref(self, key: str) -> None: ...
    async def get_pref(self, key: str, default: Any = ...) -> Any: ...
    async def list_prefs(self) -> dict[str, Any]: ...


@runtime_checkable
class TriageStore(DecisionWriter, ConsentRuleWriter, Protocol):
    """``triage_loop``: writes decisions, proposals and consent rules.

    A composed role rather than a fourth Protocol — the triage loop genuinely
    spans three concerns, and saying so is more honest than inventing one name
    that pretends it is a single responsibility.
    """

    async def put_proposal(self, p: Any) -> None: ...


__all__ = [
    "ConsentRuleReader", "ConsentRuleWriter", "DecisionReader", "DecisionWriter",
    "EventPayloadSweeper", "EventPayloadWriter", "PendingAskRegistry",
    "PrefsBackend", "ProposalWriter", "RecallReader", "ToolCallCounter",
    "TriageStore",
]
