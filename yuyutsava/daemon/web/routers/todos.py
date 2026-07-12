"""HTTP endpoints for the TODO board (docs/TODO_BOARD_PLAN.md).

CRUD over the exchange protocol — this router never touches the store or its
tables. Typed exchange exceptions map deterministically onto HTTP statuses:

  TodoValidationError → 400   TodoNotFoundError → 404
  TodoAttachmentError → 507   TodoStorageError  → 500

Responses are the versioned exchange models themselves (TodoCardV1, …) so the
REST wire format and the agent-facing contract can never drift apart.

Attachment upload (the codebase's first multipart endpoint) streams into the
card's workspace dir under a size cap, with the artifact-block registry as the
mime allowlist. Rollback covenant (plan §2): file first, row second, unlink
the file when the row write fails. Download mirrors ``routers/visuals.py``'s
FileResponse pattern.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from yuyutsava.todoboard import artifacts

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
    TodoAttachmentV1,
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


# ── attachments ────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # covers phone videos; a 413 names the cap
_UPLOAD_CHUNK = 1024 * 1024


def _safe_filename(name: str | None) -> str:
    """Basename only, filesystem-safe charset, never empty/hidden."""
    name = Path(name or "").name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .")
    return name or "upload"


def _unique_path(directory: Path, filename: str) -> Path:
    dest = directory / filename
    stem, suffix = dest.stem, dest.suffix
    counter = 1
    while dest.exists():
        dest = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return dest


async def _stream_upload(dest: Path, file: UploadFile) -> int:
    """Chunked copy to *dest*, enforcing the size cap mid-stream. The caller
    unlinks *dest* on any failure (including the 413 this raises)."""
    total = 0
    fh = await asyncio.to_thread(dest.open, "wb")
    try:
        while chunk := await file.read(_UPLOAD_CHUNK):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"attachment exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
                )
            await asyncio.to_thread(fh.write, chunk)
    finally:
        await asyncio.to_thread(fh.close)
    return total


async def _require_attachment_on_card(card_id: str, attachment_id: str) -> TodoAttachmentV1:
    """The attachment, or 404 when it isn't on this card."""
    card = await get_default_exchange().get_card(card_id)
    for att in card.attachments:
        if att.attachment_id == attachment_id:
            return att
    raise HTTPException(
        status_code=404,
        detail=f"attachment {attachment_id!r} is not on card {card_id!r}",
    )


@router.post(
    "/todos/{card_id}/attachments", response_model=TodoAttachmentV1, status_code=201,
    summary="Upload a file attachment (multipart)",
)
@_mapped
async def upload_attachment(
    card_id: str,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    kind: str | None = Form(None),
) -> TodoAttachmentV1:
    ex = get_default_exchange()
    card = await ex.get_card(card_id)  # 404 before any disk write

    mime = file.content_type or None
    if mime in (None, "application/octet-stream"):
        mime = mimetypes.guess_type(file.filename or "")[0] or mime
    if not artifacts.upload_mime_allowed(mime):
        raise HTTPException(status_code=415, detail=f"unsupported upload mime {mime!r}")
    kind = kind or artifacts.kind_for_upload(mime) or "file"

    from yuyutsava.storage.paths import blobs_dir

    workspace = Path(card.workspace_path) if card.workspace_path else blobs_dir() / "todoboard" / card_id
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    dest = _unique_path(workspace, _safe_filename(file.filename))

    # Rollback covenant: file first, row second, unlink on ANY later failure.
    try:
        size = await _stream_upload(dest, file)
        return await ex.attach(
            card_id, kind, path=str(dest), mime=mime,
            title=title or file.filename or dest.name,
            meta={"size": size, "filename": file.filename, "source": "upload"},
        )
    except BaseException:
        await asyncio.to_thread(dest.unlink, True)  # missing_ok
        raise


@router.get(
    "/todos/{card_id}/attachments/{attachment_id}",
    summary="Serve an attachment's file",
)
@_mapped
async def get_attachment(
    card_id: str,
    attachment_id: str,
    download: bool = Query(False, description="Set Content-Disposition: attachment"),
) -> FileResponse:
    att = await _require_attachment_on_card(card_id, attachment_id)
    if not att.path or not os.path.exists(att.path):
        raise HTTPException(
            status_code=404,
            detail=f"attachment {attachment_id!r} has no servable file",
        )
    return FileResponse(
        att.path,
        media_type=att.mime or "application/octet-stream",
        filename=os.path.basename(att.path) if download else None,
    )


@router.delete(
    "/todos/{card_id}/attachments/{attachment_id}", status_code=204,
    summary="Delete an attachment (row + workspace file)",
)
@_mapped
async def delete_attachment(card_id: str, attachment_id: str) -> None:
    await _require_attachment_on_card(card_id, attachment_id)
    await get_default_exchange().delete_attachment(attachment_id)
