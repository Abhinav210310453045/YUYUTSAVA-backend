"""LLM spend aggregates (Phase 4 cost tracking).

One endpoint over the ``llm_usage`` table the ``UsagePolicy``
fills: per-task, per-model, per-day or per-thread sums of tokens and estimated
USD. The per-task grouping joined against ``GET /tasks`` is the audit surface
for triage complexity noise ("complexity-1 tasks that burned 50k tokens").

``group_by=thread`` answers "what did this conversation cost?" — and is the
**only** grouping that works for chat/tinker spend on Postgres. ``task_id`` is
FK-constrained to ``tasks`` there, so a tag naming something that is not an
orchestrator task (the TinkerAgent's ``tinker:<card_id>``) is nulled on insert
and its cost lands in the anonymous bucket. ``thread_id`` carries the same
identity (``todo:<card_id>``) with no such constraint, on both backends.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from yuyutsava.daemon.web.deps import get_usage_store
from yuyutsava.daemon.web.schemas.usage import UsageOut, UsageRowOut

router = APIRouter(tags=["usage"])


@router.get(
    "/usage",
    response_model=UsageOut,
    summary="LLM token + estimated-cost aggregates",
)
async def get_usage(
    since: float | None = Query(
        None, description="Only count calls at/after this epoch-seconds timestamp",
    ),
    group_by: Literal["task", "model", "day", "thread"] | None = Query(
        None,
        description=(
            "Grouping; omit for one overall totals row. Use 'thread' for "
            "per-conversation cost (chat and tinker spend is task-less)."
        ),
    ),
    usage_store=Depends(get_usage_store),
) -> UsageOut:
    aggregates = await usage_store.aggregate(since=since, group_by=group_by)
    return UsageOut(
        since=since,
        group_by=group_by,
        rows=[
            UsageRowOut(
                key=a.key, calls=a.calls, input_tokens=a.input_tokens,
                output_tokens=a.output_tokens, est_cost_usd=a.est_cost_usd,
            )
            for a in aggregates
        ],
    )
