"""Persistence layer for yuyutsava.

This package owns every store that touches durable state: SQLite files
backing sessions, events, proposals, decisions, interrupts, preferences,
and the LangGraph checkpointer; on-disk blob directories; and the unified
TTL sweeper that ages them out.

Layout
------
- ``paths``       canonical filesystem locations for every persisted artifact
- ``ids``         thread_id minting + parsing, shared by stores and sweepers
- ``base``        BaseSqliteStore — WAL, busy_timeout, write lock, migration
- ``models``      typed records returned from store reads (filled in Step 2)
- ``sessions/``   session index store (Step 2)
- ``events/``     event payload + proposal + rule + quota stores (Step 2)
- ``interrupts``  HITL audit log (Step 2)
- ``prefs``       user preferences (Step 2)
- ``introspect``  read-only SQL execution for the debug UI
- ``sweeper``     UnifiedSweeper coordinating all TTL policies

Public boundary: nothing under ``yuyutsava.core`` may import from this
package; ``daemon/`` and ``cli/`` depend on it via dependency injection.
"""
