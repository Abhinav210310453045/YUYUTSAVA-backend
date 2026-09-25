"""A Postgres that is selected but down fails fast, by name, in one line (R-BUG-14).

With ``YUYUTSAVA_STORAGE_BACKEND=postgres`` and the server down, every CLI
command waited ten seconds and then printed a psycopg stack trace ending in
``PoolTimeout: pool initialization incomplete`` — nothing naming the host, the
port or what to do. Now the pool probes once and raises
:class:`PostgresUnavailable` with libpq's reason, the checkpointer does the
same for its own connection, and the CLI turns it into one message.

Run:  .venv/bin/python test/storage/test_postgres_unavailable.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import socket
import time
import unittest
from unittest import mock

import psycopg

from yuyutsava.cli.cli import Subcommand, dispatch
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.pg.pool import PgPool, PostgresUnavailable, describe_dsn
from yuyutsava.storage.sessions.checkpointer import build_checkpointer
from yuyutsava.storage.sessions.config import SessionsSettings

SECRET = "s3cret-pw"


def _closed_port() -> int:
    """A local port nothing listens on: bound, then released."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dead_dsn() -> str:
    return f"postgresql://yuyutsava:{SECRET}@127.0.0.1:{_closed_port()}/probe_db"


class TheError(unittest.TestCase):
    def test_it_names_host_port_and_database_but_not_the_password(self) -> None:
        exc = PostgresUnavailable(
            "postgresql://u:hunter2@db.example:6543/app",
            psycopg.OperationalError(
                'connection failed: connection to server at "10.0.0.1", port 6543 failed: '
                "Connection refused\nIs the server running?"
            ),
        )
        self.assertEqual(
            str(exc), "Postgres at db.example:6543/app is unavailable (Connection refused)"
        )
        self.assertNotIn("hunter2", str(exc))

    def test_it_is_still_an_operational_error(self) -> None:
        """So the spillover routing and every existing handler still see a
        connectivity failure."""
        self.assertTrue(issubclass(PostgresUnavailable, psycopg.OperationalError))

    def test_describe_dsn_fills_defaults_and_survives_garbage(self) -> None:
        self.assertEqual(describe_dsn("postgresql://u:p@/db"), "localhost:5432/db")
        self.assertEqual(describe_dsn("host=h port=1 dbname=d password=x"), "h:1/d")
        self.assertEqual(describe_dsn("not a = dsn = at all"), "<unparseable DSN>")


class ThePoolFailsFast(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_server_raises_at_once_with_the_reason(self) -> None:
        pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_dead_dsn()))
        started = time.monotonic()
        with self.assertRaises(PostgresUnavailable) as caught:
            await pool.open(timeout_sec=10.0)
        self.assertLess(time.monotonic() - started, 5.0, "waited out the pool timeout")
        self.assertIn("127.0.0.1:", str(caught.exception))
        self.assertIn("/probe_db", str(caught.exception))
        self.assertIn("refused", str(caught.exception).lower())
        self.assertNotIn(SECRET, str(caught.exception))


class TheCheckpointerSaysTheSame(unittest.IsolatedAsyncioTestCase):
    async def test_its_own_connection_is_translated(self) -> None:
        storage = StorageSettings(backend="postgres", pg_dsn=_dead_dsn())
        with self.assertRaises(PostgresUnavailable):
            async with build_checkpointer(SessionsSettings.from_env(), storage):
                self.fail("the checkpointer opened against a closed port")


class TheCliSaysItInOneMessage(unittest.TestCase):
    def _run(self, env: dict[str, str]) -> tuple[int, str]:
        exc = PostgresUnavailable(_dead_dsn(), psycopg.OperationalError("x: Connection refused"))

        def boom(_argv: list[str]) -> int:
            raise exc

        err = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            rc = dispatch(["boom"], commands=(Subcommand("boom", "", boom),))
        return rc, err.getvalue()

    def test_exit_2_with_the_database_and_what_to_do(self) -> None:
        rc, err = self._run({"YUYUTSAVA_STORAGE_REQUIRE": ""})
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        self.assertIn("/probe_db is unavailable (Connection refused)", err)
        self.assertIn("YUYUTSAVA_STORAGE_BACKEND=postgres selects it", err)
        self.assertIn("docker compose up -d postgres", err)
        self.assertNotIn(SECRET, err)

    def test_under_require_it_names_require(self) -> None:
        _, err = self._run({"YUYUTSAVA_STORAGE_REQUIRE": "1"})
        self.assertIn("YUYUTSAVA_STORAGE_REQUIRE forbids a SQLite fallback", err)

    def test_async_paths_are_covered_too(self) -> None:
        """The task/chat path runs under ``_quietly``; the error passes through it."""

        def boom(_argv: list[str]) -> int:
            async def go() -> int:
                raise PostgresUnavailable("postgresql:///d", OSError("Connection refused"))

            return asyncio.run(go())

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = dispatch(["boom"], commands=(Subcommand("boom", "", boom, quiet_interrupt=True),))
        self.assertEqual(rc, 2)
        self.assertIn("is unavailable (Connection refused)", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
