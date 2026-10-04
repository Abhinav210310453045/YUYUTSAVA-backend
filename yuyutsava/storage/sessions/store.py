"""Abstract session store contract.

Callers depend only on this Protocol — swapping backends (SQLite today,
Postgres later) is a single factory change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from yuyutsava.storage.models import ModelChoice, Session


class SessionNotFound(KeyError):
    """Raised by ``SessionStore.get`` when no row matches the given id."""


def model_choice_columns(choice: ModelChoice) -> tuple[str, str]:
    """*choice* as the ``model_ref`` and ``subagent_models`` columns both backends store."""
    return choice.model, json.dumps(
        dict(choice.subagents), sort_keys=True
    ) if choice.subagents else ""


def model_choice_from_columns(model_ref: str, subagent_models: str) -> ModelChoice:
    """The :class:`ModelChoice` the two columns hold."""
    subagents = json.loads(subagent_models) if subagent_models else {}
    return ModelChoice(model=model_ref, subagents=tuple(sorted(subagents.items())))


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

    async def set_title_if_empty(self, session_id: str, title: str) -> None: ...

    async def set_model_choice(self, session_id: str, choice: ModelChoice) -> None:
        """Replace the conversation's model choice; :class:`SessionNotFound` when there is none."""
        ...

    async def list_thread_family(self, base: str, *, limit: int = 50) -> list[Session]: ...
