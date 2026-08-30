"""Rolling per-thread compaction summaries, persisted across restarts.

Every time :class:`YuyutsavaCompactionMiddleware` condenses a thread, the
produced summary lands here as a new version. The latest version is what
makes "cycle 3 still knows the plan" survive checkpoint sweeps and daemon
crashes: on resume with an empty history the middleware re-injects it.

Same two-backend shape as :mod:`yuyutsava.context.artifacts`.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.summary_store")


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    version: int
    summary: str
    token_count: int
    task_id: str | None


class ThreadSummaryStore(ABC):
    @abstractmethod
    async def put(
        self,
        thread_id: str,
        summary: str,
        *,
        token_count: int = 0,
        task_id: str | None = None,
    ) -> int:
        """Append a new summary version for the thread; returns the version."""

    @abstractmethod
    async def latest(self, thread_id: str) -> ThreadSummary | None:
        """Most recent summary for the thread, or ``None``."""


# ---------------------------------------------------------------------------
# NOTE: SqliteThreadSummaryStore and PgThreadSummaryStore lived here until
# 2026-08-08. Both were replaced by ``summary_store_unified.py``
# (ADR-002 step 2.5b) — one implementation over the dialect adapter.
#
# The migration also FIXED a bug the old PgThreadSummaryStore had: concurrent
# put() calls on one thread raced on version allocation and one lost with
# UniqueViolation, because COALESCE(MAX(version),0)+1 reads a transaction
# snapshot even inside a single INSERT...SELECT. The unified store retries on
# duplicate-key. See test_summary_store_parity.py, which demonstrated the twin
# failing and the replacement passing before the twins were deleted.
#
# This module keeps the shared vocabulary: ThreadSummary and the
# ThreadSummaryStore interface.
# ---------------------------------------------------------------------------
