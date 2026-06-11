"""Semantic long-term memory (pgvector) for the agent.

What gets remembered:

- every compaction summary (``kind="summary"``, written by
  :class:`yuyutsava.context.compaction.YuyutsavaCompactionMiddleware`),
- task outcomes (``kind="task_outcome"``, written by the orchestrator loop
  next to its existing decision record),
- durable facts the agent saves explicitly via the ``mem_save`` tool
  (``kind="fact"`` / ``"preference"``).

What recalls it:

- :class:`yuyutsava.memory.injector.MemoryInjector` at task start (top-k
  similar memories rendered into the orchestrator system prompt), and
- the ``mem_search`` tool on demand.

Semantic search needs the Postgres backend; on SQLite the store degrades to
keyword matching (documented limitation, still useful).
"""

from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.store import MemoryStore, PgMemoryStore, SqliteMemoryStore

__all__ = ["MemorySettings", "MemoryStore", "PgMemoryStore", "SqliteMemoryStore"]
