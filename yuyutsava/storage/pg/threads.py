"""The ``threads`` relational hub: the parent every context table points to.

Phase 7 unified the previously FK-less islands — ``tasks``, ``llm_usage``,
``artifacts``, ``thread_summaries``, ``memories`` — under a single ``threads``
row keyed by ``thread_id`` (the same id LangGraph checkpoints and Langfuse
sessions use). :func:`~yuyutsava.storage.dialect.ensure_thread` is the
idempotent upsert every child write calls *first*, so the foreign keys added in
migration v5 are always satisfied no matter which subsystem observes a thread
first. It lives in :mod:`yuyutsava.storage.dialect`, beside
``PostgresDialect.ensure_parent``, so the dialect layer does not import this
package; this module adds the pool-level convenience.

``langfuse_session_id`` is seeded to the ``thread_id`` itself: tracing wires
Langfuse ``session_id = thread_id`` (see :mod:`yuyutsava.llm.tracing`), so the
cost-ledger ↔ observability bridge is an identity, not extra plumbing.

This lives only on the Postgres path; the zero-config SQLite backend keeps its
flat, FK-less tables unchanged.
"""

from __future__ import annotations

from yuyutsava.storage.dialect import ensure_thread
from yuyutsava.storage.pg.pool import PgPool


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
