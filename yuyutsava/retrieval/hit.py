"""Generic semantic-search result.

Domain stores (memory, skills, …) return ``Hit`` from the shared pgvector
engine. Each store may project it onto a domain-specific result type (e.g.
``MemoryHit``) for backward compatibility, but the retrieval machinery and the
:class:`~yuyutsava.retrieval.injector.RetrievalInjector` only ever speak ``Hit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Hit:
    id: str
    text: str            # the renderable payload (memory text / skill description)
    score: float         # cosine similarity in [0,1] on pg; 0.0 on the keyword twin
    payload: dict[str, Any] = field(default_factory=dict)  # extra columns (kind, scope, agent, name…)
