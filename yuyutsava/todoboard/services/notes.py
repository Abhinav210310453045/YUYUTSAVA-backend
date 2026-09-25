"""Notes: the user's and the agent's thinking on a card.

Notes are also the board's only retrieval surface, so the recall index is
written from here — inside the write path — rather than from callers who would
each have to remember.
"""

from __future__ import annotations

import logging
import time

from ulid import ULID

from yuyutsava.todoboard import validate
from yuyutsava.todoboard.errors import TodoNotFoundError, require
from yuyutsava.todoboard.models import TodoNoteV1
from yuyutsava.todoboard.services.core import BoardCore

logger = logging.getLogger("yuyutsava.todoboard.exchange")


class NoteService:
    """Write, edit, assign, search and drop notes."""

    def __init__(self, core: BoardCore) -> None:
        self._core = core

    async def add(
        self,
        card_id: str,
        body: str,
        *,
        author: str = "user",
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> TodoNoteV1:
        body = validate.body(body)
        validate.author(author)
        if phase is not None:
            validate.phase(phase)
        if objective_id is not None:
            obj = await self._core.require_objective(objective_id)
            require(
                obj.card_id == card_id,
                f"objective {objective_id} lives on card {obj.card_id}, not {card_id}",
            )
        now = time.time()
        note = TodoNoteV1(
            note_id=f"tdn_{ULID()}",
            card_id=card_id,
            body=body,
            author=author,
            objective_id=objective_id,
            phase=phase,
            created_ts=now,
            updated_ts=now,
        )
        if not await self._core.guard(self._core.store.add_note(note), "add note"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        self._schedule_index(note)
        if objective_id is not None:
            await self._core.emit(
                card_id,
                "note_assigned",
                objective_id=objective_id,
                payload={"note_id": note.note_id, "phase": phase},
                actor=author,
            )
        return note

    async def update(self, note_id: str, body: str) -> TodoNoteV1:
        body = validate.body(body)
        note = await self._core.guard(
            self._core.store.update_note(note_id, body, time.time()), "update note"
        )
        if note is None:
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")
        self._schedule_index(note, replace=True)
        return note

    async def assign(
        self,
        note_id: str,
        *,
        objective_id: str | None,
        phase: str | None = None,
        actor: str = "user",
    ) -> TodoNoteV1:
        """Attach a note to an objective (or clear with objective_id=None).
        No reindex — the body is unchanged, only the assignment moves."""
        if phase is not None:
            validate.phase(phase)
        if objective_id is not None:
            obj = await self._core.require_objective(objective_id)
            # Cross-card guard BEFORE the write: the objective's hydrated card
            # already carries its notes, so membership is one read.
            card = await self._core.require_card(obj.card_id)
            require(
                any(n.note_id == note_id for n in card.notes),
                f"objective {objective_id} lives on card {obj.card_id}, "
                f"which has no note {note_id!r}",
            )
        note = await self._core.guard(
            self._core.store.assign_note(note_id, objective_id, phase, time.time()),
            "assign note",
        )
        if note is None:
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")
        if objective_id is not None:
            await self._core.emit(
                note.card_id,
                "note_assigned",
                objective_id=objective_id,
                payload={"note_id": note_id, "phase": phase},
                actor=actor,
            )
        return note

    async def search(self, query: str, *, k: int = 8, card_id: str | None = None) -> list:
        """Semantic recall over note bodies (``todo_note_chunks``), optionally
        scoped to one card. Returns retrieval ``Hit``s whose payload carries
        ``card_id``/``note_id``. Empty when no index is wired (SQLite-only
        deployments) — recall is an enhancement, never a dependency."""
        query = (query or "").strip()
        require(bool(query), "search query must be non-empty")
        require(1 <= k <= 50, "k must be between 1 and 50")

        index = self._core.note_index
        if index is None or not index.enabled:
            return []
        return await index.search(query, k, card_id=card_id)

    async def delete(self, note_id: str) -> None:
        if not await self._core.guard(self._core.store.delete_note(note_id), "delete note"):
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")

    def _schedule_index(self, note: TodoNoteV1, *, replace: bool = False) -> None:
        """Embed a written note into the recall index, best-effort.

        A missing index (SQLite mode) or degraded Postgres is a silent skip;
        the boot sync repairs the gap.
        """
        try:
            index = self._core.note_index
            if index is not None:
                index.schedule(note, replace=replace)
        except Exception:  # noqa: BLE001 — indexing must never break a write
            logger.debug("todo: note index scheduling failed", exc_info=True)


__all__ = ["NoteService"]
