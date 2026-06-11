"""Postgres backend plumbing: connection pool + forward-only migrations.

Only active when ``YUYUTSAVA_STORAGE_BACKEND=postgres`` (see
:mod:`yuyutsava.storage.backend`). SQLite remains the zero-config default;
nothing in this package is imported on the SQLite path.
"""

from yuyutsava.storage.pg.pool import PgPool

__all__ = ["PgPool"]
