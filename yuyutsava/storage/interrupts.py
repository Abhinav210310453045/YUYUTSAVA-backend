"""SQLite-backed audit log for HITL interrupts.

Records every permission prompt / user-question interrupt across both
invocation modes (``cli`` and ``daemon``) so we can later query "for session
X, what interrupts happened and which agent asked?" without scraping logs.

The DB is dedicated (see :func:`yuyutsava.storage.paths.interrupts_db_path`)
and uses proper relational columns so a future migration into a unified
events DB is a copy-table away.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

import aiosqlite

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.models import InterruptRecord

if TYPE_CHECKING:
    from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.interrupts")


class InterruptsStore(ABC):
    """Backend-agnostic interface for the HITL audit log."""

    @abstractmethod
    async def record(self, record: InterruptRecord) -> str: ...

    @abstractmethod
    async def resolve(
        self, row_id: str, *, outcome: str, user_response: str | None = None
    ) -> None: ...

    @abstractmethod
    async def mark_orphaned_for_session(self, session_id: str) -> int: ...

    @abstractmethod
    async def list_for_session(
        self, session_id: str, *, limit: int = 100
    ) -> list[InterruptRecord]: ...

    @abstractmethod
    async def list_recent(
        self, *, agent_path_prefix: str | None = None, limit: int = 50
    ) -> list[InterruptRecord]: ...



# NOTE: SqliteInterruptsStore was replaced on 2026-08-09 by UnifiedInterruptsStore in
# storage/interrupts_unified.py (ADR-002 step 2.5b). The best-effort write
# contract is preserved and tested. Parity verified on both live backends in
# test/storage/test_interrupts_store_parity.py.



# ---------------------------------------------------------------------------
# Postgres twin
# ---------------------------------------------------------------------------




# NOTE: PgInterruptsStore was replaced on 2026-08-09 by UnifiedInterruptsStore in
# storage/interrupts_unified.py (ADR-002 step 2.5b). The best-effort write
# contract is preserved and tested. Parity verified on both live backends in
# test/storage/test_interrupts_store_parity.py.

