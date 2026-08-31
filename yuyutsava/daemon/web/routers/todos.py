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

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import FileResponse

from yuyutsava.todoboard import artifacts

from yuyutsava.daemon.web.bundle import bundle_asset_response
from yuyutsava.daemon.web.schemas.session import SessionOut
from yuyutsava.daemon.web.schemas.todo import (
    GenerateIn,
    NoteAssignIn,
    NoteIn,
    NotePatchIn,
    ObjectiveIn,
    ObjectivePatchIn,
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
from yuyutsava.storage.ids import tinker_thread_base
from yuyutsava.storage.sessions import get_default_session_store
from yuyutsava.todoboard.models import (
    BoardSnapshotV1,
    CardStatus,
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)

logger = logging.getLogger("yuyutsava.daemon.web.routers.todos")


def board(request: Request) -> TodoExchange:
    """The TODO board for this request.

    Phase 3 step 3.4. Nineteen handlers in this module each called
    ``get_default_exchange()`` in their body — a service locator invoked once
    per request, so the dependency appeared in no signature and no handler could
    be exercised against a different board.

    As a FastAPI dependency it resolves from ``app.state`` when the daemon
    installed one and falls back to the process global otherwise, which keeps
    the standalone paths working. Overriding it in a test becomes
    ``app.dependency_overrides[board] = ...`` instead of patching a module
    global and remembering to restore it.
    """
    injected = getattr(request.app.state, "todo_exchange", None)
    return injected if injected is not None else get_default_exchange()


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
    ex: TodoExchange = Depends(board),
) -> list[TodoCardSummaryV1]:
    return await ex.query_board(status=status, tag=tag, limit=limit)


# Declared before /todos/{card_id} so "snapshot" never parses as a card id.
@router.get("/todos/snapshot", response_model=BoardSnapshotV1, summary="Whole-board snapshot")
@_mapped
async def board_snapshot(
    ex: TodoExchange = Depends(board),
) -> BoardSnapshotV1:
    return await ex.board_snapshot()


# Also before /todos/{card_id} so "attachments" never parses as a card id.
@router.get(
    "/todos/attachments", response_model=list[TodoAttachmentV1],
    summary="List every card's attachments (newest first) for the gallery",
)
@_mapped
async def list_all_attachments(
    limit: int = Query(200, ge=1, le=1000),
    ex: TodoExchange = Depends(board),
) -> list[TodoAttachmentV1]:
    return await ex.list_all_attachments(limit=limit)


@router.post("/todos", response_model=TodoCardV1, status_code=201, summary="Create a TODO card")
@_mapped
async def create_todo(body: TodoCreateIn,
    ex: TodoExchange = Depends(board),
) -> TodoCardV1:
    return await ex.add_card(
        body.title, status=body.status, tags=body.tags,
        pinned=body.pinned, note=body.note,
    )


@router.get("/todos/{card_id}", response_model=TodoCardV1, summary="Read one card in full")
@_mapped
async def get_todo(card_id: str,
    ex: TodoExchange = Depends(board),
) -> TodoCardV1:
    return await ex.get_card(card_id)


@router.patch("/todos/{card_id}", response_model=TodoCardV1, summary="Update card fields")
@_mapped
async def patch_todo(card_id: str, body: TodoPatchIn,
    ex: TodoExchange = Depends(board),
) -> TodoCardV1:
    return await ex.update_card(
        card_id, title=body.title, status=body.status,
        pinned=body.pinned, tags=body.tags,
    )


@router.delete("/todos/{card_id}", status_code=204, summary="Delete a card")
@_mapped
async def delete_todo(card_id: str,
    ex: TodoExchange = Depends(board),
) -> None:
    await ex.delete_card(card_id)


@router.post(
    "/todos/{card_id}/notes", response_model=TodoNoteV1, status_code=201,
    summary="Add a note to a card",
)
@_mapped
async def add_note(card_id: str, body: NoteIn,
    ex: TodoExchange = Depends(board),
) -> TodoNoteV1:
    return await ex.add_note(
        card_id, body.body, author=body.author,
        objective_id=body.objective_id, phase=body.phase,
    )


@router.patch(
    "/todos/{card_id}/notes/{note_id}", response_model=TodoNoteV1,
    summary="Edit a note's body",
)
@_mapped
async def patch_note(card_id: str, note_id: str, body: NotePatchIn,
    ex: TodoExchange = Depends(board),
) -> TodoNoteV1:
    await _require_note_on_card(card_id, note_id, ex)
    return await ex.update_note(note_id, body.body)


@router.delete(
    "/todos/{card_id}/notes/{note_id}", status_code=204, summary="Delete a note",
)
@_mapped
async def delete_note(card_id: str, note_id: str,
    ex: TodoExchange = Depends(board),
) -> None:
    await _require_note_on_card(card_id, note_id, ex)
    await ex.delete_note(note_id)


@router.patch(
    "/todos/{card_id}/notes/{note_id}/assign", response_model=TodoNoteV1,
    summary="Assign a note to an objective (or clear with objective_id=null)",
)
@_mapped
async def assign_note(card_id: str, note_id: str, body: NoteAssignIn,
    ex: TodoExchange = Depends(board),
) -> TodoNoteV1:
    await _require_note_on_card(card_id, note_id, ex)
    return await ex.assign_note(
        note_id, objective_id=body.objective_id, phase=body.phase,
    )


async def _require_note_on_card(card_id: str, note_id: str, ex: TodoExchange) -> None:
    """404 (before any write) unless the note exists on this card."""
    card = await ex.get_card(card_id)
    if not any(n.note_id == note_id for n in card.notes):
        raise HTTPException(
            status_code=404, detail=f"note {note_id!r} is not on card {card_id!r}"
        )


# ── objectives (think flow) ────────────────────────────────────────────

@router.post(
    "/todos/{card_id}/objectives", response_model=TodoObjectiveV1, status_code=201,
    summary="Add an objective to a card's think flow",
)
@_mapped
async def add_objective(card_id: str, body: ObjectiveIn,
    ex: TodoExchange = Depends(board),
) -> TodoObjectiveV1:
    return await ex.add_objective(
        card_id, body.title, phase=body.phase,
    )


@router.patch(
    "/todos/{card_id}/objectives/{objective_id}", response_model=TodoObjectiveV1,
    summary="Update an objective (title/phase/order/reason/outcome)",
)
@_mapped
async def patch_objective(
    card_id: str, objective_id: str, body: ObjectivePatchIn,
    ex: TodoExchange = Depends(board),
) -> TodoObjectiveV1:
    await _require_objective_on_card(card_id, objective_id, ex)
    return await ex.update_objective(
        objective_id, title=body.title, phase=body.phase,
        order_idx=body.order_idx, reason=body.reason, outcome=body.outcome,
    )


@router.delete(
    "/todos/{card_id}/objectives/{objective_id}", status_code=204,
    summary="Delete an objective (its notes demote to general)",
)
@_mapped
async def delete_objective(card_id: str, objective_id: str,
    ex: TodoExchange = Depends(board),
) -> None:
    await _require_objective_on_card(card_id, objective_id, ex)
    await ex.delete_objective(objective_id)


@router.get(
    "/todos/{card_id}/events", response_model=list[TodoEventV1],
    summary="A card's activity timeline (oldest first)",
)
@_mapped
async def list_events(
    card_id: str,
    limit: int = Query(500, ge=1, le=5000),
    ex: TodoExchange = Depends(board),
) -> list[TodoEventV1]:
    await ex.get_card(card_id)  # 404 for unknown cards, not an empty list
    return await ex.list_events(card_id, limit=limit)


@router.get(
    "/todos/{card_id}/chats", response_model=list[SessionOut],
    summary="The card's tinker chat sessions, newest first",
)
@_mapped
async def list_card_chats(
    card_id: str,
    limit: int = Query(50, ge=1, le=200),
    ex: TodoExchange = Depends(board),
) -> list[SessionOut]:
    await ex.get_card(card_id)  # 404 for unknown cards
    store = get_default_session_store()
    rows = await store.list_thread_family(
        tinker_thread_base(card_id), limit=limit
    )
    return [SessionOut.from_session(s) for s in rows]


@router.post(
    "/todos/{card_id}/generate", response_model=TodoAttachmentV1, status_code=201,
    summary="Generate an artifact on the card via a generative block",
)
@_mapped
async def generate_artifact(card_id: str, body: GenerateIn,
    ex: TodoExchange = Depends(board),
) -> TodoAttachmentV1:
    return await ex.generate_artifact(
        card_id, body.block, body.spec, title=body.title,
    )


async def _require_objective_on_card(card_id: str, objective_id: str, ex: TodoExchange) -> None:
    """404 (before any write) unless the objective exists on this card."""
    card = await ex.get_card(card_id)
    if not any(o.objective_id == objective_id for o in card.objectives):
        raise HTTPException(
            status_code=404,
            detail=f"objective {objective_id!r} is not on card {card_id!r}",
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


async def _require_attachment_on_card(card_id: str, attachment_id: str, ex: TodoExchange) -> TodoAttachmentV1:
    """The attachment, or 404 when it isn't on this card."""
    card = await ex.get_card(card_id)
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
    objective_id: str | None = Form(None),
    note_id: str | None = Form(None),
    ex: TodoExchange = Depends(board),
) -> TodoAttachmentV1:
    card = await ex.get_card(card_id)  # 404 before any disk write

    # Soft association only: rows stay card-level; the objective/note pointer
    # lives in meta (with a title snapshot) so it survives objective deletion
    # the same way todo_events.objective_id does.
    assoc: dict = {}
    if objective_id:
        obj = next((o for o in card.objectives if o.objective_id == objective_id), None)
        if obj is None:
            raise HTTPException(
                status_code=400,
                detail=f"objective {objective_id!r} is not on card {card_id!r}",
            )
        assoc["objective_id"] = objective_id
        assoc["objective_title"] = obj.title
    if note_id:
        if not any(n.note_id == note_id for n in card.notes):
            raise HTTPException(
                status_code=400,
                detail=f"note {note_id!r} is not on card {card_id!r}",
            )
        assoc["note_id"] = note_id

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
            meta={"size": size, "filename": file.filename, "source": "upload", **assoc},
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
    ex: TodoExchange = Depends(board),
) -> FileResponse:
    att = await _require_attachment_on_card(card_id, attachment_id, ex)
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


@router.get(
    "/todos/{card_id}/attachments/{attachment_id}/bundle/{rel_path:path}",
    summary="Serve a file from the attachment's own directory (multi-file artifacts)",
)
@_mapped
async def get_attachment_bundle_asset(
    card_id: str, attachment_id: str, rel_path: str,
    ex: TodoExchange = Depends(board),
) -> FileResponse:
    """Bytes for one file of a multi-file artifact, relative to the attachment.

    The renderer frames the primary file at ``…/bundle/<basename of att.path>``,
    so its relative refs (``./support.js``, a sibling stylesheet) resolve back
    into this route and the artifact renders as it would from a folder on disk.
    Path containment is enforced in ``web/bundle.py``.
    """
    att = await _require_attachment_on_card(card_id, attachment_id, ex)
    return bundle_asset_response(att.path, rel_path)


@router.delete(
    "/todos/{card_id}/attachments/{attachment_id}", status_code=204,
    summary="Delete an attachment (row + workspace file)",
)
@_mapped
async def delete_attachment(card_id: str, attachment_id: str,
    ex: TodoExchange = Depends(board),
) -> None:
    await _require_attachment_on_card(card_id, attachment_id, ex)
    await ex.delete_attachment(attachment_id)
