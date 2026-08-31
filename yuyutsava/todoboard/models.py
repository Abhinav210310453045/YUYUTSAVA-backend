"""Versioned exchange schemas for the TODO board.

These Pydantic models are the ONLY contract producers/consumers see: the REST
layer returns them, the ``todo_*`` tools render them, agents read them. Each
carries an explicit ``schema_version`` so a V2 can ship alongside V1 later
without breaking existing writers — new fields go in a new model, never as
silent mutations of these.

Rows in the store map 1:1 onto these models (the store returns them directly;
there is no separate dataclass layer to keep in sync).

Versioning covenant: additive OPTIONAL fields with defaults may land inside a
version (old serialized JSON still validates, old readers ignore the extras);
any breaking change — removed, retyped, or newly-required field — ships as V2.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

CardStatus = Literal["inbox", "active", "done", "archived"]
NoteAuthor = Literal["user", "tinker", "master"]
AttachmentKind = Literal["file", "image", "video", "link", "diagram", "artifact"]
# An objective's think-flow position. The main line is thinking → planning →
# doing → completed; blocked/abandoned are off-ramps. Transitions are
# free-form (any → any) — history lives in todo_events, not in a matrix.
ObjectivePhase = Literal["thinking", "planning", "doing", "completed", "blocked", "abandoned"]

CARD_STATUSES: tuple[str, ...] = ("inbox", "active", "done", "archived")
NOTE_AUTHORS: tuple[str, ...] = ("user", "tinker", "master")
ATTACHMENT_KINDS: tuple[str, ...] = ("file", "image", "video", "link", "diagram", "artifact")
OBJECTIVE_PHASES: tuple[str, ...] = (
    "thinking", "planning", "doing", "completed", "blocked", "abandoned"
)
# Validated at the exchange, deliberately NOT CHECK'd in the DB — the
# vocabulary grows with the board (journey doc, future automations).
EVENT_KINDS: tuple[str, ...] = (
    "card_status", "objective_created", "objective_phase", "objective_updated",
    "objective_deleted", "note_assigned", "artifact_attached", "journey_generated",
)
EVENT_ACTORS: tuple[str, ...] = ("user", "tinker", "master", "system")

MAX_TITLE_LEN = 500
MAX_NOTE_LEN = 50_000
MAX_REASON_LEN = 2_000


class TodoNoteV1(BaseModel):
    schema_version: Literal[1] = 1
    note_id: str
    card_id: str
    body: str
    author: NoteAuthor = "user"
    # Think-flow assignment: which objective this note serves (None = a
    # card-level "general note") and the phase context it was written in.
    # phase is kept even after the objective is deleted (FK SET NULL) —
    # it's historical context, not a live pointer.
    objective_id: str | None = None
    phase: ObjectivePhase | None = None
    created_ts: float
    updated_ts: float


class TodoObjectiveV1(BaseModel):
    """One step of a card's think flow — a small, independently-checkable
    sub-goal that moves through OBJECTIVE_PHASES."""

    schema_version: Literal[1] = 1
    objective_id: str
    card_id: str
    title: str
    phase: ObjectivePhase = "thinking"
    order_idx: int = 0                # display order ("order" is an SQL keyword)
    reason: str | None = None         # why blocked/abandoned
    outcome: str | None = None        # what completing it produced
    created_ts: float
    updated_ts: float


class TodoEventV1(BaseModel):
    """One line of a card's activity timeline. objective_id is a soft pointer
    (no FK) — history must survive objective deletion."""

    schema_version: Literal[1] = 1
    event_id: str
    card_id: str
    objective_id: str | None = None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "user"
    created_ts: float


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
    objectives: list[TodoObjectiveV1] = Field(default_factory=list)
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
    objective_count: int = 0
    objective_done_count: int = 0     # phase == "completed"
    created_ts: float
    updated_ts: float


class BoardSnapshotV1(BaseModel):
    """The whole board at a point in time — what a master agent reads to learn
    about the user's TODOs without touching tables."""

    schema_version: Literal[1] = 1
    generated_ts: float = Field(default_factory=time.time)
    cards: list[TodoCardSummaryV1] = Field(default_factory=list)
