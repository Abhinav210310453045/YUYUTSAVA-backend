"""Semantic long-term memory for agents, on pgvector.

What is remembered: compaction summaries (``kind="summary"``, written by
:class:`~yuyutsava.context.compaction.YuyutsavaCompactionMiddleware`), task
outcomes (``kind="task_outcome"``, written by the orchestrator loop), and facts or
preferences an agent saves with the ``mem_save`` tool.

What recalls it: :class:`~yuyutsava.context.injector.MemoryInjector` at the start
of a task, and the ``mem_search`` tool on demand.

Semantic search needs the Postgres backend; on SQLite the store falls back to
keyword matching. This package re-exports nothing: import a name from the module
that owns it.
"""
