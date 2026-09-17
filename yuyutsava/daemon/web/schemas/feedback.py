"""Pydantic schemas for the message-feedback endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from yuyutsava.storage.feedback_store import MessageFeedback


class FeedbackIn(BaseModel):
    session_id: str = Field(..., description="Session the message belongs to")
    message_ref: str = Field(..., description="Client message id / turn ref being rated")
    rating: str = Field(..., description="'up' or 'down'")
    user_text: str = Field("", description="Snapshot of the prompting user turn")
    assistant_text: str = Field("", description="Snapshot of the rated assistant turn")
    note: str | None = Field(None, description="Optional free-text note (e.g. why 👎)")


class FeedbackOut(BaseModel):
    feedback_id: str
    thread_id: str
    session_id: str
    message_ref: str
    rating: str
    note: str | None
    user_text: str
    assistant_text: str
    created_ts: float

    @classmethod
    def from_record(cls, r: MessageFeedback) -> "FeedbackOut":
        return cls(
            feedback_id=r.feedback_id,
            thread_id=r.thread_id,
            session_id=r.session_id,
            message_ref=r.message_ref,
            rating=r.rating,
            note=r.note,
            user_text=r.user_text,
            assistant_text=r.assistant_text,
            created_ts=r.created_ts,
        )
