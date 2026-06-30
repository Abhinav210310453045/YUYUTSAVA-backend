"""Abstract session store contract.

Callers depend only on this Protocol — swapping backends (SQLite today,
Postgres later) is a single factory change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from yuyutsava.storage.models import Session


class SessionNotFound(KeyError):
    """Raised by ``SessionStore.get`` when no row matches the given id."""


@runtime_checkable
class SessionStore(Protocol):
    async def create(
        self,
        *,
        workspace: Path,
        task: str,
        thread_id: str | None = None,
        origin: str = "cli",
    ) -> Session: ...

    async def get(self, session_id: str) -> Session: ...

    async def list(
        self,
        *,
        workspace: Path | None = None,
        limit: int = 100,
        order_by: str = "updated_at",
        cursor: float | None = None,
        origin: str | None = None,
    ) -> list[Session]: ...

    async def touch(
        self,
        session_id: str,
        *,
        message_delta: int = 0,
        memory_files_count: int | None = None,
        task_preview: str | None = None,
    ) -> None: ...

    async def update_status(self, session_id: str, status: str) -> None: ...

    async def delete(self, session_id: str) -> None: ...
