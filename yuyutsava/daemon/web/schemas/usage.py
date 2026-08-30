"""Pydantic schemas for the usage endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UsageRowOut(BaseModel):
    key: str = Field(
        description="Group key: task_id, model name, YYYY-MM-DD day, "
                    "or 'all' when ungrouped",
    )
    calls: int
    input_tokens: int
    output_tokens: int
    est_cost_usd: float


class UsageOut(BaseModel):
    since: float | None = Field(
        None, description="Epoch-seconds lower bound that was applied (if any)",
    )
    group_by: Literal["task", "model", "day", "thread"] | None = None
    rows: list[UsageRowOut] = Field(
        description="Aggregates, most expensive group first",
    )
