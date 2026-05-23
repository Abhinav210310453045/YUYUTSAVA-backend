"""Pydantic schema for the sessions endpoints."""

from __future__ import annotations

from pydantic import BaseModel

from yuyutsava.storage.models import Session


class SessionOut(BaseModel):
    id: str
    thread_id: str
    workspace: str
    status: str
    created_at: float
    updated_at: float
    message_count: int
    memory_files_count: int
    db_row_bytes: int
    task_preview: str
    schema_version: int

    @classmethod
    def from_session(cls, s: Session) -> "SessionOut":
        return cls(
            id=s.id,
            thread_id=s.thread_id,
            workspace=str(s.workspace),
            status=s.status,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=s.message_count,
            memory_files_count=s.memory_files_count,
            db_row_bytes=s.db_row_bytes,
            task_preview=s.task_preview,
            schema_version=s.schema_version,
        )
