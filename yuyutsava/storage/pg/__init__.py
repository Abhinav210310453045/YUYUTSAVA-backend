"""Postgres plumbing: the connection pool (:mod:`~yuyutsava.storage.pg.pool`) and
forward-only schema migrations (:mod:`~yuyutsava.storage.pg.migrations`).

Used only when ``YUYUTSAVA_STORAGE_BACKEND=postgres`` (see
:mod:`yuyutsava.storage.backend`); SQLite is the zero-config default. This package
re-exports nothing: import a name from the module that owns it.
"""
