"""Versioned exchange schemas for the TODO board.

These Pydantic models are the ONLY contract producers/consumers see: the REST
layer returns them, the ``todo_*`` tools render them, agents read them. Each
carries an explicit ``schema_version`` so a V2 can ship alongside V1 later
without breaking existing writers — new fields go in a new model, never as
silent mutations of these.

Rows in the store map 1:1 onto these models (the store returns them directly;
there is no separate dataclass layer to keep in sync).
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

CardStatus = Literal["inbox", "active", "done", "archived"]
NoteAuthor = Literal["user", "tinker", "master"]
AttachmentKind = Literal["file", "image", "video", "link", "diagram", "artifact"]

CARD_STATUSES: tuple[str, ...] = ("inbox", "active", "done", "archived")
NOTE_AUTHORS: tuple[str, ...] = ("user", "tinker", "master")
ATTACHMENT_KINDS: tuple[str, ...] = ("file", "image", "video", "link", "diagram", "artifact")

MAX_TITLE_LEN = 500
MAX_NOTE_LEN = 50_000


class TodoNoteV1(BaseModel):
    schema_version: Literal[1] = 1
    note_id: str
    card_id: str
    body: str
    author: NoteAuthor = "user"
    created_ts: float
    updated_ts: float


class TodoAttachmentV1(BaseModel):
    schema_version: Literal[1] = 1
    attachment_id: str
    card_id: str
    kind: AttachmentKind
    path: str | None = None      # on-disk file (inside the card's workspace)
    url: str | None = None       # external resource (kind == "link")
    mime: str | None = None
    title: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_ts: float


class TodoCardV1(BaseModel):
    """One fully hydrated card: title + notes + attachments."""

    schema_version: Literal[1] = 1
    card_id: str
    title: str
    status: CardStatus = "inbox"
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)
    workspace_path: str | None = None
    created_ts: float
    updated_ts: float
    notes: list[TodoNoteV1] = Field(default_factory=list)
    attachments: list[TodoAttachmentV1] = Field(default_factory=list)


class TodoCardSummaryV1(BaseModel):
    """Board-listing view of a card — counts instead of hydrated children."""

    schema_version: Literal[1] = 1
    card_id: str
    title: str
    status: CardStatus
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)
    note_count: int = 0
    attachment_count: int = 0
    created_ts: float
    updated_ts: float


class BoardSnapshotV1(BaseModel):
    """The whole board at a point in time — what a master agent reads to learn
    about the user's TODOs without touching tables."""

    schema_version: Literal[1] = 1
    generated_ts: float = Field(default_factory=time.time)
    cards: list[TodoCardSummaryV1] = Field(default_factory=list)
