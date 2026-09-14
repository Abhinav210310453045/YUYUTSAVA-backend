"""Spillover routing: Postgres is the sole writer while healthy; SQLite buffers writes while it is down.

On a Postgres runtime error a write is redirected to SQLite and the process is
marked degraded (:mod:`~yuyutsava.storage.routing.health`). A background probe clears
the flag on recovery, and :mod:`~yuyutsava.storage.routing.reconcile` drains the
buffered rows back into Postgres (``INSERT ... ON CONFLICT DO NOTHING``) and deletes
them from SQLite, so a row never lives in both places. With no Postgres configured,
nothing routes through here: SQLite is the only store.

This package re-exports nothing: import a name from the module that owns it.
"""
