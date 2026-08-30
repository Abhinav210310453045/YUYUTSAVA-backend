"""Every Postgres timestamp column is ``TIMESTAMPTZ``. One rule, enforced.

Migration v20, closing **finding AK**.

Before v20 the schema carried two conventions with no stated principle: 22
columns were `TIMESTAMPTZ` and 19 were `DOUBLE PRECISION` epoch seconds. That
made `Dialect.ts_param()` and `Dialect.epoch()` correct **per column** rather
than per backend — knowledge that lived nowhere, and produced an insert that
failed with:

    column "created_at" is of type double precision
    but expression is of type timestamp with time zone

The type choice is now uniform, so the helpers are always right. This suite is
what keeps it that way: a future migration that adds a `DOUBLE PRECISION`
timestamp fails here rather than at the first write on whichever backend nobody
was testing.

SQLite keeps REAL epoch seconds — it has no timestamp type at all. That
asymmetry is precisely what the dialect exists to absorb, and it is asserted
here too, so "unify the backends" never gets misread as "give SQLite
timestamps".

Run:  .venv/bin/python test/storage/test_timestamp_convention.py
"""

from __future__ import annotations

import ast
import os
import pathlib
import socket
import unittest
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN


def _pg_dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_reachable() -> bool:
    u = urlparse(_pg_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()

#: Columns whose name looks temporal. Kept as a name test rather than a
#: hand-maintained list so a new table is covered the day it is added.
_TEMPORAL = ("_ts", "_at")


def _is_temporal(column: str) -> bool:
    return column == "ts" or column.endswith(_TEMPORAL)


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUsesTimestamptzEverywhere(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def _columns(self) -> list[tuple[str, str, str]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema = 'public'"
            )
            rows = await cur.fetchall()
        return [(t, c, d) for t, c, d in rows if _is_temporal(c)]

    async def test_no_temporal_column_is_double_precision(self) -> None:
        cols = await self._columns()
        self.assertTrue(cols, "found no temporal columns at all — query is wrong")
        offenders = [f"{t}.{c}" for t, c, d in cols if d == "double precision"]
        self.assertEqual(
            offenders, [],
            "these Postgres timestamp columns are DOUBLE PRECISION, not "
            "TIMESTAMPTZ:\n  " + "\n  ".join(offenders)
            + "\n\nMigration v20 unified the convention so Dialect.ts_param() "
              "and Dialect.epoch() are always correct. A column that opts out "
              "brings back finding AK: the helpers become a per-column choice "
              "again, and picking wrong fails only on Postgres, only at "
              "runtime.",
        )

    async def test_every_temporal_column_is_timestamptz(self) -> None:
        """Stronger than the above: no INTEGER or NUMERIC sneaking in either."""
        cols = await self._columns()
        wrong = [
            f"{t}.{c} ({d})" for t, c, d in cols
            if not d.startswith("timestamp")
        ]
        self.assertEqual(
            wrong, [],
            "temporal columns with a non-timestamp type:\n  " + "\n  ".join(wrong),
        )

    async def test_the_count_is_what_v20_produced(self) -> None:
        """A ratchet, so an unreviewed schema change is visible.

        41 = the 22 that were always TIMESTAMPTZ + the 19 v20 converted. Raise
        it when you add a table; if it *drops*, a table was lost.
        """
        self.assertEqual(
            len(await self._columns()), 41,
            "the number of Postgres temporal columns changed. Adding a table is "
            "fine — update this number. A decrease means something was dropped.",
        )


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class TimestamptzResolutionIsMicroseconds(unittest.IsolatedAsyncioTestCase):
    """The one thing migration v20 cost, stated as a property rather than found later.

    ``DOUBLE PRECISION`` stored an epoch float bit-exactly. ``TIMESTAMPTZ``
    resolves to **1 microsecond**, so a round-trip rounds:
    ``1786248210.0348558`` comes back ``1786248210.034856``.

    No real information is lost — ``time.time()`` itself resolves to roughly a
    microsecond on Linux and macOS, and the extra digits are float
    representation, not measurement. What *is* gone is bit-exact equality, so
    anything comparing a stored timestamp to its input must use a tolerance.
    Pinned here so the bound is a known number, not a surprise in whichever
    test trips over it next.
    """

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_round_trip_is_accurate_to_one_microsecond(self) -> None:
        import time as _t

        original = _t.time()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT extract(epoch FROM to_timestamp(%s))::float8", (original,))
            back = float((await cur.fetchone())[0])
        self.assertAlmostEqual(
            back, original, delta=1e-6,
            msg=f"a timestamp round-trip lost more than 1 us: "
                f"{original!r} -> {back!r}",
        )

    async def test_ordering_survives_the_rounding(self) -> None:
        """Rounding must not reorder rows written a millisecond apart."""
        import time as _t

        base = _t.time()
        vals = [base, base + 0.001, base + 0.002]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT extract(epoch FROM to_timestamp(v))::float8 "
                "FROM unnest(%s::float8[]) AS v ORDER BY v", (vals,))
            back = [float(r[0]) for r in await cur.fetchall()]
        self.assertEqual(back, sorted(back))
        self.assertEqual(len(set(back)), 3, "distinct timestamps collapsed")


class SqliteKeepsEpochReals(unittest.TestCase):
    """SQLite has no timestamp type; the schemas must not pretend otherwise.

    Read straight off the ``_SCHEMA_SQL`` of every SQLite schema owner, because
    that is the artefact that would drift.
    """

    def _schema_owners(self):
        root = pathlib.Path(__file__).resolve().parents[2] / "yuyutsava"
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if (isinstance(stmt, ast.AnnAssign)
                            and getattr(stmt.target, "id", "") == "_SCHEMA_SQL"
                            and isinstance(stmt.value, ast.Constant)):
                        yield path.name, node.name, stmt.value.value
                    elif (isinstance(stmt, ast.Assign)
                          and any(getattr(t, "id", "") == "_SCHEMA_SQL" for t in stmt.targets)
                          and isinstance(stmt.value, ast.Constant)):
                        yield path.name, node.name, stmt.value.value

    def test_no_sqlite_schema_declares_a_timestamp_type(self) -> None:
        offenders: list[str] = []
        for filename, cls, sql in self._schema_owners():
            for line in sql.splitlines():
                low = line.lower()
                if "timestamp" in low or "timestamptz" in low:
                    offenders.append(f"{filename}:{cls}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "a SQLite schema declares a timestamp type:\n  "
            + "\n  ".join(offenders)
            + "\n\nSQLite has no such type — it would be stored as TEXT and "
              "compare lexically against the epoch floats every caller passes.",
        )

    def test_schema_owners_were_actually_found(self) -> None:
        """Negative control: the test above is vacuous if the scan finds nothing."""
        owners = list(self._schema_owners())
        self.assertGreaterEqual(
            len(owners), 5,
            f"only found {len(owners)} SQLite schema owners; the AST scan is "
            f"not reaching them, so the check above proves nothing",
        )


class DialectHelpersDocumentTheRule(unittest.TestCase):
    def test_epoch_casts_to_float8(self) -> None:
        """Without the cast a timestamp reads as Decimal on PG and float on SQLite."""
        from yuyutsava.storage.dialect import PostgresDialect, SqliteDialect

        pg = PostgresDialect.__new__(PostgresDialect)
        self.assertIn(
            "::float8", pg.epoch("created_ts"),
            "epoch() dropped its float8 cast — extract(epoch ...) returns "
            "numeric, which psycopg hands back as Decimal, so Decimal == float "
            "comparisons silently fail",
        )
        sq = SqliteDialect.__new__(SqliteDialect)
        self.assertEqual(sq.epoch("created_ts"), "created_ts")

    def test_epoch_supports_a_qualified_column(self) -> None:
        """``d.ts`` is not a legal alias, so a qualified column needs one given."""
        from yuyutsava.storage.dialect import PostgresDialect

        pg = PostgresDialect.__new__(PostgresDialect)
        self.assertEqual(
            pg.epoch("d.ts", "ts"), "extract(epoch FROM d.ts)::float8 AS ts")


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
