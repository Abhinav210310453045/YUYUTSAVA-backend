"""The TODO-board exchange protocol — the ONLY write/read path to the board.

Every producer/consumer (REST router, ``todo_*`` tools, master agent,
TinkerAgent) talks to :class:`TodoExchange`; nothing else may touch the store
or its tables. What that contract guarantees:

  * validation (status/author/kind vocabularies, title/body limits) — raising
    :class:`TodoValidationError` before the store ever sees bad data;
  * the per-card **workspace** under ``blobs_dir()/todoboard/<card_id>/`` —
    created on ``add_card``, removed on ``delete_card`` (rows first via
    CASCADE, then the dir; a re-run after a partial failure is a no-op);
  * typed exceptions so callers map failures deterministically (the REST layer
    → HTTP status codes, tools → structured error strings). Storage faults are
    wrapped in :class:`TodoStorageError` so agent loops and routers never
    crash on a backend hiccup.

The work lives in :mod:`yuyutsava.todoboard.services`, one module per domain,
and this class is the facade over them: it owns the public method names and
the one thing no single domain can own, which is the order two domains happen
in. Callers see no change from that — the facade *is* the protocol, and adding
a method here without a service to put it in is the smell to watch for.
"""

from __future__ import annotations

from typing import Any

from yuyutsava.todoboard.errors import (
    TodoAttachmentError,
    TodoError,
    TodoNotFoundError,
    TodoStorageError,
    TodoValidationError,
)
from yuyutsava.todoboard.models import (
    BoardSnapshotV1,
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)
from yuyutsava.todoboard.services import (
    AttachmentService,
    BoardCore,
    CardService,
    NoteService,
    ObjectiveService,
    workspace_root,
)
from yuyutsava.todoboard.store import DEFAULT_LIST_LIMIT, TodoStore


def board_workspace_root():
    """Root of every card workspace (``blobs/todoboard/``) — the containment
    zone for agents that work across cards (the background TinkerAgent)."""
    return workspace_root()


class TodoExchange:
    """Versioned protocol facade over one :class:`TodoStore`."""

    def __init__(self, store: TodoStore, *, note_index: Any | None = None) -> None:
        """One board: *store* holds it, *note_index* (optional) recalls its notes."""
        core = BoardCore(store, note_index)
        self._core = core
        self._cards = CardService(core)
        self._objectives = ObjectiveService(core)
        self._notes = NoteService(core)
        self._attachments = AttachmentService(core)

    @property
    def note_index(self) -> Any | None:
        """The index that recalls this board's notes, or ``None`` (SQLite-only)."""
        return self._core.note_index

    # ── cards ──────────────────────────────────────────────────────────

    async def add_card(
        self,
        title: str,
        *,
        status: str = "inbox",
        tags: list[str] | None = None,
        pinned: bool = False,
        note: str | None = None,
        note_author: str = "user",
    ) -> TodoCardV1:
        """Create a card (optionally with a first note) and its workspace dir.

        The note is written here rather than by the card service: it is the one
        place two domains meet, and the order is the guarantee — no card, no
        note, so the note cannot outlive a failed card.
        """
        card = await self._cards.add(title, status=status, tags=tags, pinned=pinned)
        if note:
            card.notes.append(await self._notes.add(card.card_id, note, author=note_author))
        return card

    async def get_card(self, card_id: str) -> TodoCardV1:
        return await self._cards.get(card_id)

    async def update_card(
        self,
        card_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
        actor: str = "user",
    ) -> TodoCardV1:
        return await self._cards.update(
            card_id, title=title, status=status, pinned=pinned, tags=tags, actor=actor
        )

    async def delete_card(self, card_id: str) -> None:
        await self._cards.delete(card_id)

    async def query_board(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TodoCardSummaryV1]:
        return await self._cards.query(status=status, tag=tag, limit=limit)

    async def board_snapshot(self) -> BoardSnapshotV1:
        return await self._cards.snapshot()

    async def list_card_ids(self) -> list[str]:
        return await self._cards.list_ids()

    # ── objectives (think flow) ────────────────────────────────────────

    async def add_objective(
        self,
        card_id: str,
        title: str,
        *,
        phase: str = "thinking",
        reason: str | None = None,
        actor: str = "user",
    ) -> TodoObjectiveV1:
        return await self._objectives.add(card_id, title, phase=phase, reason=reason, actor=actor)

    async def update_objective(
        self,
        objective_id: str,
        *,
        title: str | None = None,
        phase: str | None = None,
        order_idx: int | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        actor: str = "user",
    ) -> TodoObjectiveV1:
        return await self._objectives.update(
            objective_id,
            title=title,
            phase=phase,
            order_idx=order_idx,
            reason=reason,
            outcome=outcome,
            actor=actor,
        )

    async def delete_objective(self, objective_id: str, *, actor: str = "user") -> None:
        await self._objectives.delete(objective_id, actor=actor)

    # ── notes ──────────────────────────────────────────────────────────

    async def add_note(
        self,
        card_id: str,
        body: str,
        *,
        author: str = "user",
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> TodoNoteV1:
        return await self._notes.add(
            card_id, body, author=author, objective_id=objective_id, phase=phase
        )

    async def update_note(self, note_id: str, body: str) -> TodoNoteV1:
        return await self._notes.update(note_id, body)

    async def assign_note(
        self,
        note_id: str,
        *,
        objective_id: str | None,
        phase: str | None = None,
        actor: str = "user",
    ) -> TodoNoteV1:
        return await self._notes.assign(
            note_id, objective_id=objective_id, phase=phase, actor=actor
        )

    async def search_notes(self, query: str, *, k: int = 8, card_id: str | None = None) -> list:
        return await self._notes.search(query, k=k, card_id=card_id)

    async def delete_note(self, note_id: str) -> None:
        await self._notes.delete(note_id)

    # ── attachments ────────────────────────────────────────────────────

    async def attach(  # noqa: PLR0913  the published protocol's shape, fixed by its callers
        self,
        card_id: str,
        kind: str,
        *,
        path: str | None = None,
        url: str | None = None,
        mime: str | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> TodoAttachmentV1:
        return await self._attachments.attach(
            card_id,
            kind,
            path=path,
            url=url,
            mime=mime,
            title=title,
            meta=meta,
            actor=actor,
        )

    async def generate_artifact(
        self,
        card_id: str,
        block: str,
        spec: dict[str, Any] | None = None,
        *,
        title: str | None = None,
        actor: str = "user",
    ) -> TodoAttachmentV1:
        return await self._attachments.generate(card_id, block, spec, title=title, actor=actor)

    async def delete_attachment(self, attachment_id: str) -> None:
        await self._attachments.delete(attachment_id)

    async def list_all_attachments(self, *, limit: int = 200) -> list[TodoAttachmentV1]:
        return await self._attachments.list_all(limit=limit)

    # ── timeline ───────────────────────────────────────────────────────

    async def list_events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        return await self._core.events(card_id, limit=limit)


__all__ = [
    "TodoExchange",
    "board_workspace_root",
    "TodoError",
    "TodoValidationError",
    "TodoNotFoundError",
    "TodoStorageError",
    "TodoAttachmentError",
]
