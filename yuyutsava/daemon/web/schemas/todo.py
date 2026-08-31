"""Pydantic request schemas for the TODO-board endpoints.

Responses reuse the versioned exchange models
(:mod:`yuyutsava.todoboard.models`) directly — they ARE the wire contract —
so only the request bodies live here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from yuyutsava.todoboard.models import CardStatus, NoteAuthor, ObjectivePhase


class TodoCreateIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    status: CardStatus = "inbox"
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    note: str | None = Field(
        None, description="Optional first note added with the card",
    )


class TodoPatchIn(BaseModel):
    """Partial update — only the fields present change."""

    title: str | None = Field(None, min_length=1, max_length=500)
    status: CardStatus | None = None
    pinned: bool | None = None
    tags: list[str] | None = None


class NoteIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=50_000)
    author: NoteAuthor = "user"
    objective_id: str | None = Field(
        None, description="Attach the note to this objective on the same card",
    )
    phase: ObjectivePhase | None = None


class NotePatchIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=50_000)


class NoteAssignIn(BaseModel):
    """Move a note onto an objective (or clear with objective_id=null)."""

    objective_id: str | None = None
    phase: ObjectivePhase | None = None


class ObjectiveIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    phase: ObjectivePhase = "thinking"


class ObjectivePatchIn(BaseModel):
    """Partial update — only the fields present change."""

    title: str | None = Field(None, min_length=1, max_length=500)
    phase: ObjectivePhase | None = None
    order_idx: int | None = None
    reason: str | None = Field(None, max_length=2_000)
    outcome: str | None = Field(None, max_length=2_000)


class GenerateIn(BaseModel):
    """Generate an artifact on the card via a generative block."""

    block: str = Field(..., min_length=1)
    spec: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
