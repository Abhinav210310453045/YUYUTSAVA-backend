"""The Postgres tests never run against the application's database.

Two parity tests exercise a TTL sweep — ``delete_older_than(time.time() + 3600)``
on the transcript and artifact stores — which empties the whole table. The
tests' database used to default to the application's own, so every run of the
suite without a scratch DSN deleted the user's transcripts and artifacts (the
lossless read-back had nothing left to read). The tests now have a database of
their own, and a DSN naming the application's database is refused.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from test.support import pg
from yuyutsava.storage.backend import DEFAULT_PG_DSN


def _db(dsn: str) -> str:
    return pg.database_name(dsn)


class TheTestsHaveTheirOwnDatabase(unittest.TestCase):
    def test_by_default_the_tests_use_a_database_that_is_not_the_apps(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(pg.TEST_DSN_ENV, None)
            os.environ.pop("YUYUTSAVA_PG_DSN", None)
            self.assertNotEqual(_db(pg.pg_dsn()), _db(DEFAULT_PG_DSN))
            self.assertFalse(pg.is_application_database(pg.pg_dsn()))

    def test_the_apps_default_database_is_refused(self) -> None:
        self.assertTrue(pg.is_application_database(DEFAULT_PG_DSN))

    def test_whatever_database_the_app_is_configured_for_is_refused(self) -> None:
        app = "postgresql://u:p@127.0.0.1:5435/my_real_board"
        with mock.patch.dict(os.environ, {"YUYUTSAVA_PG_DSN": app}):
            self.assertTrue(pg.is_application_database(app))
            self.assertTrue(
                pg.is_application_database("postgresql://other:x@localhost:5435/my_real_board")
            )

    def test_the_apps_variable_no_longer_steers_the_tests(self) -> None:
        app = "postgresql://u:p@127.0.0.1:5435/yuyutsava"
        with mock.patch.dict(os.environ, {"YUYUTSAVA_PG_DSN": app}):
            os.environ.pop(pg.TEST_DSN_ENV, None)
            self.assertNotEqual(_db(pg.pg_dsn()), "yuyutsava")


if __name__ == "__main__":
    unittest.main()
