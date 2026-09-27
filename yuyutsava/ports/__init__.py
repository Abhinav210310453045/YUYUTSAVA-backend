"""Dependency-free protocols: types that both sides of a would-be cycle can import.

**The one rule: this package imports nothing from ``yuyutsava``,** so any package can
depend on it without creating a cycle. ``test/architecture/test_ports_is_a_leaf.py`` enforces it.
Protocols are structural: an implementation satisfies one without inheriting from it.
The decision is ADR-003.

- :mod:`~yuyutsava.ports.agent` — ``Agent``, what the driver needs from a compiled agent
- :mod:`~yuyutsava.ports.ask` — ``AskUser``, asking the user without knowing how the
  answer arrives
- :mod:`~yuyutsava.ports.channels` — the event payloads, ``AskPrompt``, ``UserChannel``
  and ``AskRouter``: what the daemon sends to the user and how
- :mod:`~yuyutsava.ports.policy` — ``CapEnforcer``, ``RuntimeToggles``, ``ContextTuning``,
  ``TaskMirror``, ``RemoteSubagentSpec``
- :mod:`~yuyutsava.ports.retrieval` — ``ConversationIndex``
- :mod:`~yuyutsava.ports.storage` — ``ArtifactStore``, ``MemoryStore``, ``SummaryStore``,
  ``TranscriptStore``, ``UsageStore``
- :mod:`~yuyutsava.ports.tool_events` — ``ToolEventRule``, ``ToolEventRegistry``: how a
  tool result becomes a typed stream event, without the driver naming the tools

The names below are re-exported because consumers import them from the package root;
import anything else from the module that owns it.
"""

from yuyutsava.ports.agent import Agent
from yuyutsava.ports.policy import CapEnforcer, RuntimeToggles
from yuyutsava.ports.retrieval import ConversationIndex
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
    "CapEnforcer",
    "ConversationIndex",
    "MemoryStore",
    "SummaryStore",
    "RuntimeToggles",
    "TranscriptStore",
    "UsageStore",
]
