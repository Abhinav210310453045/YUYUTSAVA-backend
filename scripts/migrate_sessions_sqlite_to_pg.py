"""One-time migration: copy CLI sessions from SQLite into Postgres.

After switching to ``YUYUTSAVA_STORAGE_BACKEND=postgres``, run this once to
carry existing ``~/.yuyutsava/sessions.db`` rows into the new Postgres
``sessions`` table (migration v6) so the daemon and CLI share one durable,
JOINable session index. Idempotent — ``ON CONFLICT (id) DO NOTHING`` — and the
SQLite file is left untouched as a backup.

Usage::

    YUYUTSAVA_STORAGE_BACKEND=postgres python scripts/migrate_sessions_sqlite_to_pg.py
"""

from __future__ import annotations

import asyncio

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread
from yuyutsava.storage.sessions.config import SessionsSettings
from yuyutsava.storage.sessions.sqlite_impl import SqliteSessionStore

_INSERT_SQL = """
INSERT INTO sessions
    (id, thread_id, workspace, status, created_at, updated_at,
     message_count, memory_files_count, db_row_bytes, task_preview,
     schema_version)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


async def main() -> None:
    storage = StorageSettings.from_env()
    if not storage.is_postgres():
        print("YUYUTSAVA_STORAGE_BACKEND is not postgres — nothing to do.")
        return

    settings = SessionsSettings.from_env()
    src = SqliteSessionStore(
        settings.db_path, busy_timeout_ms=settings.busy_timeout_ms
    )
    rows = await src.list(limit=1_000_000)
    if not rows:
        print(f"No sessions found in {settings.db_path}.")
        return

    pool = PgPool(storage)
    await pool.open()
    try:
        await pg_migrations.apply(pool)  # ensure threads + sessions tables exist
        migrated = 0
        async with pool.connection() as conn:
            for s in rows:
                ws = str(s.workspace)
                # sessions.thread_id FKs to threads — upsert the parent first.
                await ensure_thread(
                    conn, s.thread_id, origin="cli", workspace=ws, status=s.status
                )
                cur = await conn.execute(
                    _INSERT_SQL,
                    (s.id, s.thread_id, ws, s.status, s.created_at, s.updated_at,
                     s.message_count, s.memory_files_count, s.db_row_bytes,
                     s.task_preview, s.schema_version),
                )
                migrated += cur.rowcount or 0
        print(
            f"Migrated {migrated} new session(s) into Postgres "
            f"({len(rows)} found in SQLite; existing ids skipped)."
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
