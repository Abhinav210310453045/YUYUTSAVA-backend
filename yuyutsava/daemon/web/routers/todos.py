"""HTTP endpoints for the TODO board (docs/TODO_BOARD_PLAN.md).

CRUD over the exchange protocol — this router never touches the store or its
tables. Typed exchange exceptions map deterministically onto HTTP statuses:

  TodoValidationError → 400   TodoNotFoundError → 404
  TodoAttachmentError → 507   TodoStorageError  → 500

Responses are the versioned exchange models themselves (TodoCardV1, …) so the
REST wire format and the agent-facing contract can never drift apart.
Attachment upload/download is a later phase; ``link`` attachments and rows for
agent-written files are already representable through the card model.
"""

from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable, TypeVar

from fastapi import APIRouter, HTTPException, Query

from yuyutsava.daemon.web.schemas.todo import (
    NoteIn,
    NotePatchIn,
    TodoCreateIn,
    TodoPatchIn,
)
from yuyutsava.todoboard.exchange import (
    TodoAttachmentError,
    TodoError,
    TodoNotFoundError,
    TodoValidationError,
    get_default_exchange,
)
from yuyutsava.todoboard.models import (
    BoardSnapshotV1,
    CardStatus,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoNoteV1,
)

logger = logging.getLogger("yuyutsava.daemon.web.routers.todos")

router = APIRouter(tags=["todos"])

_STATUS_OF = (
    (TodoValidationError, 400),
    (TodoNotFoundError, 404),
    (TodoAttachmentError, 507),
    (TodoError, 500),
)

T = TypeVar("T")


def _mapped(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Translate exchange exceptions into HTTPException per the table above."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except TodoError as exc:
            for exc_type, code in _STATUS_OF:
                if isinstance(exc, exc_type):
                    raise HTTPException(status_code=code, detail=str(exc)) from exc
            raise  # unreachable — TodoError is the last row

    return wrapper


@router.get("/todos", response_model=list[TodoCardSummaryV1], summary="List TODO cards")
@_mapped
async def list_todos(
    status: CardStatus | None = Query(None, description="Filter by card status"),
    tag: str | None = Query(None, description="Filter to cards carrying this tag"),
    limit: int = Query(500, ge=1, le=5000),
) -> list[TodoCardSummaryV1]:
    return await get_default_exchange().query_board(status=status, tag=tag, limit=limit)


# Declared before /todos/{card_id} so "snapshot" never parses as a card id.
@router.get("/todos/snapshot", response_model=BoardSnapshotV1, summary="Whole-board snapshot")
@_mapped
async def board_snapshot() -> BoardSnapshotV1:
    return await get_default_exchange().board_snapshot()


@router.post("/todos", response_model=TodoCardV1, status_code=201, summary="Create a TODO card")
@_mapped
async def create_todo(body: TodoCreateIn) -> TodoCardV1:
    return await get_default_exchange().add_card(
        body.title, status=body.status, tags=body.tags,
        pinned=body.pinned, note=body.note,
    )


@router.get("/todos/{card_id}", response_model=TodoCardV1, summary="Read one card in full")
@_mapped
async def get_todo(card_id: str) -> TodoCardV1:
    return await get_default_exchange().get_card(card_id)


@router.patch("/todos/{card_id}", response_model=TodoCardV1, summary="Update card fields")
@_mapped
async def patch_todo(card_id: str, body: TodoPatchIn) -> TodoCardV1:
    return await get_default_exchange().update_card(
        card_id, title=body.title, status=body.status,
        pinned=body.pinned, tags=body.tags,
    )


@router.delete("/todos/{card_id}", status_code=204, summary="Delete a card")
@_mapped
async def delete_todo(card_id: str) -> None:
    await get_default_exchange().delete_card(card_id)


@router.post(
    "/todos/{card_id}/notes", response_model=TodoNoteV1, status_code=201,
    summary="Add a note to a card",
)
@_mapped
async def add_note(card_id: str, body: NoteIn) -> TodoNoteV1:
    return await get_default_exchange().add_note(card_id, body.body, author=body.author)


@router.patch(
    "/todos/{card_id}/notes/{note_id}", response_model=TodoNoteV1,
    summary="Edit a note's body",
)
@_mapped
async def patch_note(card_id: str, note_id: str, body: NotePatchIn) -> TodoNoteV1:
    await _require_note_on_card(card_id, note_id)
    return await get_default_exchange().update_note(note_id, body.body)


@router.delete(
    "/todos/{card_id}/notes/{note_id}", status_code=204, summary="Delete a note",
)
@_mapped
async def delete_note(card_id: str, note_id: str) -> None:
    await _require_note_on_card(card_id, note_id)
    await get_default_exchange().delete_note(note_id)


async def _require_note_on_card(card_id: str, note_id: str) -> None:
    """404 (before any write) unless the note exists on this card."""
    card = await get_default_exchange().get_card(card_id)
    if not any(n.note_id == note_id for n in card.notes):
        raise HTTPException(
            status_code=404, detail=f"note {note_id!r} is not on card {card_id!r}"
        )
