"""Per-domain store ABCs for the events database.

The monolithic :class:`~yuyutsava.storage.events.store.Store` historically owned
every table on one SQLite connection. Phase 2 splits it into one ABC per
logical domain, each with a SQLite twin (``sqlite_stores``) and a Postgres twin
(``pg_stores``) — mirroring the artifacts twin
(:mod:`yuyutsava.context.artifacts`) and memory
(:mod:`yuyutsava.memory.store`) patterns.

Every method is ``async`` so the two backends share one signature and a
Postgres call never blocks the event loop. The sole exception is
:meth:`ConsentGrantStore.list_cached`, which the synchronous
``yuyutsava.consent.store.ConsentStore`` Protocol pins — it returns a cache
filled at boot by :meth:`ConsentGrantStore.load`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from yuyutsava.storage.models import (
    ConsentRule,
    Decision,
    EventRecord,
    Proposal,
)

if TYPE_CHECKING:
    from yuyutsava.consent.models import Grant


class EventStore(ABC):
    """``event_payloads`` — raw event detail referenced by id."""

    @abstractmethod
    async def put_event_payload(
        self,
        *,
        event_id: str,
        topic: str,
        ts: float,
        payload: dict[str, Any],
        blob_path: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_event_payload(self, event_id: str) -> EventRecord | None: ...

    @abstractmethod
    async def delete_event_payloads_with_blob_prefix(
        self, prefix: str, older_than_ts: float
    ) -> int: ...

    @abstractmethod
    async def delete_event_payloads_older_than(self, older_than_ts: float) -> int: ...


class ProposalStore(ABC):
    """``proposals`` — pending Tier-1 actions awaiting a decision."""

    @abstractmethod
    async def put(self, p: Proposal) -> None: ...

    @abstractmethod
    async def get(self, proposal_id: str) -> Proposal | None: ...

    @abstractmethod
    async def try_set_status(
        self, proposal_id: str, *, from_status: str, to_status: str
    ) -> bool: ...


class DecisionStore(ABC):
    """``decisions`` — the resolved-action audit log."""

    @abstractmethod
    async def put(
        self,
        *,
        proposal_id: str | None,
        event_id: str,
        outcome: str,
        action_summary: str | None = None,
        ts: float | None = None,
        session_id: str | None = None,
        agent_path: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def list(self, limit: int = 50, cursor: float | None = None) -> list[Decision]: ...

    @abstractmethod
    async def recall(
        self, topic_glob: str, since_sec: float, limit: int = 20
    ) -> list[dict[str, Any]]: ...


class ConsentRuleStore(ABC):
    """``consent_rules`` — Tier-1 auto-approve/skip rules."""

    @abstractmethod
    async def put(self, rule: ConsentRule) -> None: ...

    @abstractmethod
    async def list(self) -> list[ConsentRule]: ...


class ToolCounterStore(ABC):
    """``tool_call_counters`` — per-tool daily quotas (permission policy)."""

    @abstractmethod
    async def incr(self, tool_name: str, day: str) -> int: ...

    @abstractmethod
    async def get(self, tool_name: str, day: str) -> int: ...


class PrefsBackend(ABC):
    """``user_prefs`` — small JSON blobs keyed by dot-namespaced strings."""

    @abstractmethod
    async def put(self, key: str, value: Any) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def get(self, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    async def list(self) -> dict[str, Any]: ...


class ConsentGrantStore(ABC):
    """``consent_grants`` — the unified consent allowlist.

    ``load`` fills the boot cache returned by the synchronous ``list_cached``
    so the facade can satisfy the sync ``ConsentStore`` Protocol.
    """

    @abstractmethod
    async def put(self, grant: "Grant") -> None: ...

    @abstractmethod
    async def delete(self, grant_id: str) -> None: ...

    @abstractmethod
    async def load(self) -> list["Grant"]: ...


class PendingAskStore(ABC):
    """``pending_asks`` — Tier-2 asks awaiting an answer.

    Durable because nothing here expires: the agent is parked on a LangGraph
    interrupt and waits indefinitely, so the record has to outlive the process
    that raised it. :meth:`put` runs *before* the ask is broadcast, which is
    what makes a dropped SSE frame recoverable and a daemon restart resumable.
    """

    @abstractmethod
    async def put(self, record: dict[str, Any]) -> None:
        """Insert a pending ask. Idempotent on ``ask_id``."""

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop every pending ask for a thread. Returns rows deleted.

        Required by session deletion: an ask row stores the agent's question
        (``title``, ``body``) and the user's ``response``, so leaving it behind
        keeps conversation content from a session the user asked to delete.
        """

    @abstractmethod
    async def resolve(self, ask_id: str, response: str, *, status: str = "answered") -> bool:
        """Flip pending → answered. False when it was already resolved.

        The compare-and-set is what makes "first answer anywhere wins" safe
        across surfaces answering at the same moment.
        """

    @abstractmethod
    async def list_pending(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every unanswered ask, oldest first (the Inbox + boot hydration)."""

    @abstractmethod
    async def get(self, ask_id: str) -> dict[str, Any] | None: ...
