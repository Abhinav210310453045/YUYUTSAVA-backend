"""Abstract session store contract.

Concrete backends live next to this module (``sqlite_store.py`` today; a
SQLAlchemy/Postgres impl is the planned drop-in). Callers depend only on this
Protocol — swapping backends is a single factory change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from yuyutsava.sessions.models import Session


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
    ) -> Session: ...

    async def get(self, session_id: str) -> Session: ...

    async def list(
        self,
        *,
        workspace: Path | None = None,
        limit: int = 100,
        order_by: str = "updated_at",
    ) -> list[Session]: ...

    async def touch(
        self,
        session_id: str,
        *,
        message_delta: int = 0,
        memory_files_count: int | None = None,
    ) -> None: ...

    async def update_status(self, session_id: str, status: str) -> None: ...

    async def delete(self, session_id: str) -> None: ...
