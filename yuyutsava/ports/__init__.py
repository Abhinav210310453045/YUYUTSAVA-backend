"""Dependency-free protocols — the acyclic layer both sides of a cycle import.

Phase 3 step 3.1 (ADR-003), the root-cause fix for ``F-S05`` and ``F-K03``.

The problem is not style. ``core/`` imports from ``context/``, ``memory/``,
``skills/``, ``daemon/`` and ``agents/``, and every one of those imports back
from ``core/`` — **9 package-level cycles from top-level imports alone**, with
``core`` in five of them. There is no acyclic direction in which a type can be
declared, so the codebase worked around it twice:

* ~10 dependency fields typed ``object | None`` with the real type in a comment
  (``OrchestratorDeps``, ``BaseSubAgent.__init__``) — "untyped to avoid cycle";
* ~24% of internal imports deferred inside function bodies, 68 in
  ``core/engine.py`` alone.

Both are DIP simulated in prose. A comment saying ``# memory.store.MemoryStore``
is unverifiable and drifts silently, and deferred imports make *statement
position inside a function body* load-bearing behaviour that no tool checks.

**The one rule: this package imports nothing from ``yuyutsava``.** Both sides of
a cycle depend on it and neither on the other, so the cycle disappears
structurally rather than being evaded. Enforced by
``test/test_ports_is_a_leaf.py`` — without that guard this decays back, because
the pressure that created the cycles is still present.

Protocols are **structural**: existing stores satisfy them without inheriting
anything and without a single call site changing. Only declared types move.
"""

from yuyutsava.ports.agent import Agent
from yuyutsava.ports.ask import AskUser
from yuyutsava.ports.policy import CapEnforcer, RuntimeToggles
from yuyutsava.ports.retrieval import ConversationIndex, VectorSearcher
from yuyutsava.ports.storage import (
    ArtifactStore,
    MemoryStore,
    SummaryStore,
    TranscriptStore,
    UsageStore,
)

__all__ = [
    "Agent",
    "ArtifactStore",
    "AskUser",
    "CapEnforcer",
    "ConversationIndex",
    "MemoryStore",
    "SummaryStore",
    "RuntimeToggles",
    "TranscriptStore",
    "UsageStore",
    "VectorSearcher",
]
