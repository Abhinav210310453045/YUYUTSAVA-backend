"""Tests that need a live Postgres: which database, whether it answers, and the mark.

One copy of what 22 test files each carried. :func:`requires_pg` both skips a
test when the database is unavailable and marks it ``pg``, so the marker cannot
drift from the condition: ``pytest -m "not pg"`` deselects exactly the tests
that would otherwise skip for want of a database.

**The tests have a database of their own and never touch the application's.**
Some of them are destructive by design — the TTL-sweep parity tests call
``delete_older_than(time.time() + 3600)``, which empties the whole table. When
the tests defaulted to the application's database, every run of the suite
deleted the user's transcripts and artifacts. So:

* the DSN is ``YUYUTSAVA_TEST_PG_DSN``, else the application's server with the
  database ``yuyutsava_test``, created on first use;
* ``YUYUTSAVA_PG_DSN`` — the application's setting — never steers the tests;
* a DSN naming the application's database (its default, or whatever
  ``YUYUTSAVA_PG_DSN`` names) is refused, and the tests skip saying why.
"""

from __future__ import annotations

import os
import socket
import unittest
from typing import TypeVar
from urllib.parse import urlparse, urlunparse

import pytest

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.storage.pg.pool import describe_dsn

T = TypeVar("T")

TEST_DSN_ENV = "YUYUTSAVA_TEST_PG_DSN"
TEST_DATABASE = "yuyutsava_test"
_APP_DSN_ENV = "YUYUTSAVA_PG_DSN"


def database_name(dsn: str) -> str:
    """The database a DSN names (its path, without the leading slash)."""
    return urlparse(dsn).path.lstrip("/")


def _with_database(dsn: str, name: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{name}"))


def pg_dsn() -> str:
    """The database the Postgres tests use — never the application's."""
    return os.environ.get(TEST_DSN_ENV, "").strip() or _with_database(DEFAULT_PG_DSN, TEST_DATABASE)


def is_application_database(dsn: str) -> bool:
    """Whether *dsn* names a database the application itself uses.

    By name, whatever the host or credentials: two DSNs for the same server can
    differ in spelling, and a false refusal only costs a skipped test.
    """
    app = {database_name(DEFAULT_PG_DSN)}
    configured = os.environ.get(_APP_DSN_ENV, "").strip()
    if configured:
        app.add(database_name(configured))
    return database_name(dsn) in app


def _ensure_database(dsn: str) -> bool:
    """Connect to *dsn*, creating its database on the same server if it is missing."""
    import psycopg  # noqa: PLC0415 — only when a server answers; the hermetic run never pays for it

    try:
        psycopg.connect(dsn, connect_timeout=3).close()
        return True
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            return False
    try:
        with psycopg.connect(
            _with_database(dsn, "postgres"), autocommit=True, connect_timeout=3
        ) as conn:
            conn.execute(f'CREATE DATABASE "{database_name(dsn)}"')
        return True
    except psycopg.Error:
        return False


def _unavailable() -> str | None:
    """Why the Postgres tests cannot run, or ``None`` when they can."""
    dsn = pg_dsn()
    if is_application_database(dsn):
        return (
            f"refusing to run against the application's database {describe_dsn(dsn)}: "
            f"set {TEST_DSN_ENV} to a scratch database"
        )
    u = urlparse(dsn)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            pass
    except OSError:
        return f"no Postgres reachable at {describe_dsn(dsn)}"
    if not _ensure_database(dsn):
        return f"cannot open or create the test database {describe_dsn(dsn)}"
    return None


UNAVAILABLE = _unavailable()
PG_UP = UNAVAILABLE is None


def requires_pg(obj: T) -> T:
    """Skip *obj* (a test class or function) without a test database, and mark it ``pg``.

    The skip reason names host, port and database — never the password.
    """
    skipped = unittest.skipUnless(PG_UP, UNAVAILABLE or "")
    return pytest.mark.pg(skipped(obj))
