"""Row schema for the session index."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SESSION_STATUSES = ("running", "idle", "crashed", "done")


@dataclass(frozen=True)
class Session:
    """One persisted CLI session — the row the store hands back to callers."""

    id: str
    thread_id: str
    workspace: Path
    status: str
    created_at: float
    updated_at: float
    message_count: int
    memory_files_count: int
    db_row_bytes: int
    task_preview: str
    schema_version: int = 1
