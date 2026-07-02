"""Complete session teardown: purge every row + file tied to a thread.

Single source of truth for "delete this session", called by both the CLI
(``--delete-session``) and the daemon web endpoint (``DELETE /sessions/{id}``)
so the two paths can never drift again. Before this module they diverged: the
CLI dropped only the checkpoint + ``sessions`` rows, the web endpoint also swept
voice history, and *neither* touched the transcript, artifacts, summaries,
memory, task, usage or proposal/decision rows — those leaked forever because the
default SQLite backend spreads them across four un-joinable DB files (so there is
no cross-file cascade) and the Postgres ``threads`` hub row was never deleted (so
its ``ON DELETE CASCADE`` children never fired).

What a purge removes for a thread (``session.id == thread_id``):

* LangGraph ``checkpoints`` + ``writes`` (via the saver's ``adelete_thread``)
* ``transcript_messages`` (+ ``transcript_chunks`` on PG)
* ``artifacts`` (+ ``artifact_chunks`` on PG)
* ``thread_summaries``
* ``voice_messages`` + the on-disk ``blobs/voice/<thread_id>/`` clips
* ``tasks`` + ``llm_usage`` (cost/audit — purged in full, by request)
* ``proposals`` + ``decisions`` (keyed on ``session_id``)
* ``interrupts``
* ``memories`` — only the *ephemeral* kinds (``task_outcome``/``summary``);
  durable ``fact``/``preference`` memories the user taught survive (on PG they
  keep their row with ``source_thread_id`` nulled when the hub row is dropped)
* the ``sessions`` row (deleted **last**) and, on PG, the ``threads`` hub row

Failure model (atomic / hard-fail): every step raises on error rather than
swallowing. True single-transaction atomicity is impossible in SQLite mode — the
data spans four DB files plus on-disk blobs — so we get as close as practical:
the whole ``state.db`` group is one ``BEGIN IMMEDIATE`` transaction (PG is one
transaction too), and the ``sessions`` row is deleted **last**. Because every
delete is idempotent (``DELETE ... WHERE <id> = ?``), a mid-purge failure leaves
the session still listed, and re-running the delete cleanly finishes the job:
*"session gone" ⟺ "everything cleaned."*
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.paths import interrupts_db_path, state_db_path
from yuyutsava.storage.sessions import (
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
)

logger = logging.getLogger("yuyutsava.storage.purge")

# Durable user knowledge survives a session delete; ephemeral per-run memory
# (task outcomes, compaction summaries) is purged with the session.
KEEP_MEMORY_KINDS: tuple[str, ...] = ("fact", "preference")

# ``state.db`` tables tied to a session, with the column that carries the id.
# ``session.id == thread_id`` (see ``SqliteSessionStore.create``), so a single
# value binds every clause; the real column is named for clarity + correctness
# on the two events tables that key on ``session_id`` rather than ``thread_id``.
# Table/column names come only from this fixed whitelist, never user input, so
# the f-string interpolation below is safe.
_STATE_TABLES: tuple[tuple[str, str], ...] = (
    ("transcript_messages", "thread_id"),
    ("artifacts", "thread_id"),
    ("thread_summaries", "thread_id"),
    ("voice_messages", "thread_id"),
    ("tasks", "thread_id"),
    ("llm_usage", "thread_id"),
    ("proposals", "session_id"),
    ("decisions", "session_id"),
)

# PG child tables cleared (explicitly, not via cascade — so the purge is
# independent of constraint drift) before the ``threads`` hub row is dropped.
# Ordered so FK dependents precede their parents: ``llm_usage.task_id`` -> tasks.
_PG_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("llm_usage", "thread_id"),
    ("tasks", "thread_id"),
    ("transcript_chunks", "thread_id"),
    ("transcript_messages", "thread_id"),
    ("artifact_chunks", "thread_id"),
    ("artifacts", "thread_id"),
    ("thread_summaries", "thread_id"),
    ("voice_messages", "thread_id"),
    ("interrupts", "thread_id"),
    ("proposals", "session_id"),
    ("decisions", "session_id"),
)


@dataclass
class PurgeReport:
    """Per-target deletion counters, returned by :func:`purge_session`."""

    session_id: str
    thread_id: str
    checkpoints_deleted: bool = False
    session_row_deleted: bool = False
    voice_blobs_deleted: int = 0
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


async def purge_session(session_id: str, *, pg_pool: object | None = None) -> PurgeReport:
    """Remove every row + file tied to ``session_id``. Raises ``SessionNotFound``.

    Self-contained: reads the process-singleton session store and opens whatever
    else it needs (checkpointer, a short-lived PG pool in Postgres mode), so the
    CLI and the web endpoint call it identically with no store wiring. Any step
    failing propagates (hard-fail); see the module docstring for the ordering
    that makes a partial purge safely retryable.
    """
    store = get_default_session_store()
    s = await store.get(session_id)  # raises SessionNotFound if unknown
    thread_id = s.thread_id
    report = PurgeReport(session_id=session_id, thread_id=thread_id)
    settings = StorageSettings.from_env()

    owns_pool = False
    if settings.is_postgres() and pg_pool is None:
        from yuyutsava.storage.pg.pool import PgPool

        pg_pool = PgPool(settings)
        await pg_pool.open()
        owns_pool = True

    try:
        # 1. LangGraph checkpoint + write rows (own DB file / PG tables).
        async with build_checkpointer(SessionsSettings.from_env()) as saver:
            await saver.adelete_thread(thread_id)
        report.checkpoints_deleted = True

        # 2. Domain rows (transcript / artifacts / summaries / voice / tasks /
        #    usage / proposals / decisions / interrupts / memories).
        if settings.is_postgres():
            await _purge_pg_children(pg_pool, thread_id, session_id, report)
        else:
            await _purge_sqlite(thread_id, session_id, report)

        # 3. On-disk voice audio clips.
        from yuyutsava.audio_io.blobs import delete_thread_voice_blobs

        report.voice_blobs_deleted = delete_thread_voice_blobs(thread_id)

        # 3b. Rendered visuals — rows + on-disk image files (the DB row holds an
        #     absolute path, so a raw table delete would orphan the PNGs). Use the
        #     backend-aware default store (PgVisualStore when the daemon injected
        #     it, else the SQLite twin) so PG-primary rows are cleaned too. Run
        #     BEFORE the thread hub is dropped so files are unlinked (the FK
        #     CASCADE would only remove the row, not the on-disk PNG).
        from yuyutsava.visuals.store import get_default_visual_store

        report.rows["visual_artifacts"] = await get_default_visual_store().delete_for_thread(
            thread_id
        )

        # 4. Sessions row LAST — a failure above leaves it listed + retryable.
        await store.delete(session_id)
        report.session_row_deleted = True

        # 5. PG only: drop the now-childless ``threads`` hub. The sessions row
        #    had a NO ACTION FK to it, so this must follow store.delete; the
        #    SET NULL cascade releases the kept fact/preference memories.
        if settings.is_postgres():
            await _drop_pg_thread(pg_pool, thread_id, report)
    finally:
        if owns_pool:
            await pg_pool.close()

    logger.info(
        "purge: session=%s thread=%s rows=%d voice_blobs=%d",
        session_id, thread_id, report.total_rows, report.voice_blobs_deleted,
    )
    return report


# ---------------------------------------------------------------------------
# SQLite backend (zero-config default)
# ---------------------------------------------------------------------------


async def _sqlite_tables(conn: aiosqlite.Connection) -> set[str]:
    """Names of tables that actually exist (stores create them lazily)."""
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cur.fetchall()
    await cur.close()
    return {r[0] for r in rows}


async def _purge_sqlite(thread_id: str, session_id: str, report: PurgeReport) -> None:
    """Delete every ``state.db`` row for the thread in one transaction, then the
    interrupts row from its dedicated DB file.

    Tables absent on a fresh DB (a store never wrote them) are skipped rather
    than raising ``no such table`` — which would abort the transaction.
    """
    ident = {"thread_id": thread_id, "session_id": session_id}

    async with aiosqlite.connect(state_db_path()) as conn:
        await conn.execute("PRAGMA busy_timeout=5000")
        existing = await _sqlite_tables(conn)
        await conn.execute("BEGIN IMMEDIATE")
        try:
            for table, key in _STATE_TABLES:
                if table not in existing:
                    continue
                cur = await conn.execute(
                    f"DELETE FROM {table} WHERE {key} = ?", (ident[key],)
                )
                report.rows[table] = cur.rowcount or 0
            if "memories" in existing:
                placeholders = ",".join("?" for _ in KEEP_MEMORY_KINDS)
                cur = await conn.execute(
                    f"DELETE FROM memories WHERE source_thread_id = ? "
                    f"AND kind NOT IN ({placeholders})",
                    (thread_id, *KEEP_MEMORY_KINDS),
                )
                report.rows["memories"] = cur.rowcount or 0
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    # Interrupts live in their own DB file — a separate (still hard-fail) step.
    ipath = interrupts_db_path()
    if not ipath.exists():
        return
    async with aiosqlite.connect(ipath) as conn:
        await conn.execute("PRAGMA busy_timeout=5000")
        if "interrupts" not in await _sqlite_tables(conn):
            return
        cur = await conn.execute(
            "DELETE FROM interrupts WHERE thread_id = ?", (thread_id,)
        )
        report.rows["interrupts"] = cur.rowcount or 0
        await conn.commit()


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------


async def _pg_table_exists(conn, table: str) -> bool:
    cur = await conn.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    row = await cur.fetchone()
    return bool(row) and row[0] is not None


async def _purge_pg_children(
    pg_pool, thread_id: str, session_id: str, report: PurgeReport
) -> None:
    """Delete every child row for the thread in one transaction (hub dropped
    later, after the sessions row, in :func:`_drop_pg_thread`)."""
    ident = {"thread_id": thread_id, "session_id": session_id}
    async with pg_pool.transaction() as conn:
        if await _pg_table_exists(conn, "memories"):
            cur = await conn.execute(
                "DELETE FROM memories WHERE source_thread_id = %s "
                "AND kind <> ALL(%s)",
                (thread_id, list(KEEP_MEMORY_KINDS)),
            )
            report.rows["memories"] = cur.rowcount or 0
        for table, key in _PG_CHILD_TABLES:
            if not await _pg_table_exists(conn, table):
                continue
            cur = await conn.execute(
                f"DELETE FROM {table} WHERE {key} = %s", (ident[key],)
            )
            report.rows[table] = cur.rowcount or 0


async def _drop_pg_thread(pg_pool, thread_id: str, report: PurgeReport) -> None:
    """Drop the childless ``threads`` hub row (SET NULLs the kept memories)."""
    async with pg_pool.transaction() as conn:
        cur = await conn.execute(
            "DELETE FROM threads WHERE thread_id = %s", (thread_id,)
        )
        report.rows["threads"] = cur.rowcount or 0
