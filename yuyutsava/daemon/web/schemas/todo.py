"""Pydantic request schemas for the TODO-board endpoints.

Responses reuse the versioned exchange models
(:mod:`yuyutsava.todoboard.models`) directly — they ARE the wire contract —
so only the request bodies live here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from yuyutsava.todoboard.models import CardStatus, NoteAuthor


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


class NotePatchIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=50_000)
