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
    cache_read_tokens: int = Field(
        0, description="Input tokens served from the provider's prompt cache "
                       "(a subset of input_tokens, not an addition to it)",
    )
    cache_creation_tokens: int = Field(
        0, description="Input tokens charged for writing the prompt cache "
                       "(also a subset of input_tokens)",
    )


class UsageOut(BaseModel):
    since: float | None = Field(
        None, description="Epoch-seconds lower bound that was applied (if any)",
    )
    group_by: Literal["task", "model", "day", "thread"] | None = None
    rows: list[UsageRowOut] = Field(
        description="Aggregates, most expensive group first",
    )


class UsageSummaryOut(BaseModel):
    """Everything a usage dashboard needs for one time range, in one call.

    Three groupings the UI would otherwise fetch separately and then have to
    keep consistent with each other.
    """

    since: float | None = None
    totals: UsageRowOut = Field(
        description="One row for the whole range (key is 'all')",
    )
    by_model: list[UsageRowOut] = Field(
        default_factory=list, description="Most expensive model first",
    )
    by_day: list[UsageRowOut] = Field(
        default_factory=list,
        description="One row per YYYY-MM-DD, oldest first — a series to plot",
    )
    unpriced_models: list[str] = Field(
        default_factory=list,
        description=(
            "Models in this range with no entry in model_prices.json. Their "
            "est_cost_usd is 0, which is 'unknown', not 'free' — show it as "
            "unpriced rather than letting the total read low."
        ),
    )


class UsageSessionOut(BaseModel):
    """One conversation's spend, with whatever the session store knows of it."""

    thread_id: str
    title: str = Field("", description="Session title, or '' if none is recorded")
    origin: str = Field("", description="cli | ui | voice, or '' when unknown")
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    est_cost_usd: float
    priced: bool = Field(
        True, description="False when no model in this session has a price entry",
    )
    models: list[str] = Field(default_factory=list)
    first_ts: float
    last_ts: float


class UsageSessionsOut(BaseModel):
    since: float | None = None
    rows: list[UsageSessionOut] = Field(
        default_factory=list, description="Most recently active first",
    )
