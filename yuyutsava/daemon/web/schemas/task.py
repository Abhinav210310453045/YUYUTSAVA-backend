"""Pydantic schemas for the tasks endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TaskStatusStr = Literal["queued", "running", "done", "failed", "cancelled"]


class TaskSubmitIn(BaseModel):
    instruction: str = Field(
        ..., min_length=1, max_length=20_000,
        description="What the orchestrator should do, in plain language",
    )
    mode: Literal["direct", "triage"] = Field(
        "direct",
        description="direct = trusted, runs immediately; "
                    "triage = goes through LLM triage + Tier-1 consent",
    )
    origin: str = Field(
        "api", max_length=64,
        description="Submitting surface, recorded on the task row",
    )


class TaskSubmitOut(BaseModel):
    task_id: str
    mode: Literal["direct", "triage"]


class TaskOut(BaseModel):
    task_id: str
    origin: str
    instruction: str
    status: TaskStatusStr
    created_ts: float
    thread_id: str | None = None
    complexity: int | None = None
    started_ts: float | None = None
    finished_ts: float | None = None
    deferred_ms: int = 0
    result_summary: str | None = None
    error: str | None = None


class TaskListOut(BaseModel):
    tasks: list[TaskOut]
    next_cursor: str | None = Field(
        None, description="Pass back as ?cursor= for the next page; null at end",
    )


class TaskEventsOut(BaseModel):
    task_id: str
    events: list[dict[str, Any]] = Field(
        description="Replay of the task's StreamItems in wire format "
                    "(same envelopes the /stream SSE emits)",
    )
