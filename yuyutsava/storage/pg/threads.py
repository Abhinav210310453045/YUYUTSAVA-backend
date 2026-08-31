"""The ``threads`` relational hub: the parent every context table points to.

Phase 7 unified the previously FK-less islands — ``tasks``, ``llm_usage``,
``artifacts``, ``thread_summaries``, ``memories`` — under a single ``threads``
row keyed by ``thread_id`` (the same id LangGraph checkpoints and Langfuse
sessions use). :func:`ensure_thread` is the idempotent upsert every child
write calls *first*, so the foreign keys added in migration v5 are always
satisfied no matter which subsystem observes a thread first.

``langfuse_session_id`` is seeded to the ``thread_id`` itself: tracing wires
Langfuse ``session_id = thread_id`` (see :mod:`yuyutsava.core.tracing`), so the
cost-ledger ↔ observability bridge is an identity, not extra plumbing.

This lives only on the Postgres path; the zero-config SQLite backend keeps its
flat, FK-less tables unchanged.
"""

from __future__ import annotations

import psycopg

from yuyutsava.storage.pg.pool import PgPool

_UPSERT_SQL = """
INSERT INTO threads
    (thread_id, origin, workspace, status, title, langfuse_session_id)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (thread_id) DO NOTHING
"""


async def ensure_thread(
    conn: psycopg.AsyncConnection,
    thread_id: str | None,
    *,
    origin: str | None = None,
    workspace: str | None = None,
    status: str | None = None,
    title: str | None = None,
) -> None:
    """Upsert the parent ``threads`` row on ``conn``. No-op for falsy ids.

    Cheap and idempotent (``ON CONFLICT DO NOTHING``); the optional attributes
    are recorded only when the row is first created. Run it on the *same*
    pooled connection as the child write so the parent exists before the FK is
    checked.
    """
    if not thread_id:
        return
    await conn.execute(
        _UPSERT_SQL,
        (thread_id, origin, workspace, status, title, thread_id),
    )


async def ensure_thread_pool(
    pool: PgPool,
    thread_id: str | None,
    **attrs: str | None,
) -> None:
    """Pool-level convenience: borrow a connection and :func:`ensure_thread`."""
    if not thread_id:
        return
    async with pool.connection() as conn:
        await ensure_thread(conn, thread_id, **attrs)
