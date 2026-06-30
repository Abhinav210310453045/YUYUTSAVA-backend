"""Migration v9 applies and creates the events/interrupts tables + FKs.

Requires a live Postgres (the same one the daemon would use). When none is
reachable the whole case is skipped — CI without Postgres still passes.
"""

from __future__ import annotations

import unittest

from yuyutsava.storage.backend import StorageSettings


async def _open_pool():
    from yuyutsava.storage.pg.pool import PgPool

    settings = StorageSettings.from_env()
    # Force postgres knobs even if the env says sqlite — we only test the schema.
    from dataclasses import replace
    settings = replace(settings, backend="postgres")
    pool = PgPool(settings)
    await pool.open(timeout_sec=2.0)
    return pool


class MigrationV9Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.pool = await _open_pool()
        except Exception as exc:  # noqa: BLE001 — no Postgres in this environment
            raise unittest.SkipTest(f"no Postgres reachable: {exc}")

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_v9_tables_and_fks_exist(self) -> None:
        from yuyutsava.storage.pg import migrations as pg_migrations

        await pg_migrations.apply(self.pool)

        expected_tables = {
            "event_payloads", "proposals", "decisions", "consent_rules",
            "tool_call_counters", "user_prefs", "consent_grants", "interrupts",
        }
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(expected_tables),),
            )
            present = {r[0] for r in await cur.fetchall()}
            self.assertEqual(present, expected_tables)

            # The three load-bearing foreign keys from migration v9.
            cur = await conn.execute(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE constraint_type = 'FOREIGN KEY' AND constraint_name = ANY(%s)",
                (["proposals_event_fk", "decisions_proposal_fk", "interrupts_thread_fk"],),
            )
            fks = {r[0] for r in await cur.fetchall()}
            self.assertIn("proposals_event_fk", fks)
            self.assertIn("decisions_proposal_fk", fks)
            self.assertIn("interrupts_thread_fk", fks)


if __name__ == "__main__":
    unittest.main()
