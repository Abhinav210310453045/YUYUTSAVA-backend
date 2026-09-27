"""Tests that need a live Postgres: where it is, whether it answers, and the mark.

One copy of what 22 test files each carried. :func:`requires_pg` both skips a
test when the server does not answer and marks it ``pg``, so the marker cannot
drift from the condition: ``pytest -m "not pg"`` deselects exactly the tests
that would otherwise skip for want of a database.

The DSN is ``YUYUTSAVA_PG_DSN``, else the application's default — which is the
real database. Point ``YUYUTSAVA_PG_DSN`` at a scratch database before running
these (see test/README.md).
"""

from __future__ import annotations

import os
import socket
import unittest
from typing import TypeVar
from urllib.parse import urlparse

import pytest

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.storage.pg.pool import describe_dsn

T = TypeVar("T")


def pg_dsn() -> str:
    """The database the Postgres tests use."""
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def pg_reachable() -> bool:
    """Whether something accepts connections at :func:`pg_dsn`'s host and port."""
    u = urlparse(pg_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = pg_reachable()


def requires_pg(obj: T) -> T:
    """Skip *obj* (a test class or function) without Postgres, and mark it ``pg``.

    The skip reason names host, port and database — never the password.
    """
    skipped = unittest.skipUnless(PG_UP, f"no Postgres reachable at {describe_dsn(pg_dsn())}")
    return pytest.mark.pg(skipped(obj))
