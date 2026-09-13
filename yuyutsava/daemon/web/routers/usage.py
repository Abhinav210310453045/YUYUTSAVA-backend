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

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from yuyutsava.daemon.web.deps import get_usage_store
from yuyutsava.daemon.web.schemas.usage import (
    UsageOut,
    UsageRowOut,
    UsageSessionOut,
    UsageSessionsOut,
    UsageSummaryOut,
)

logger = logging.getLogger("yuyutsava.daemon.web.usage")

router = APIRouter(tags=["usage"])


def _row(a: Any) -> UsageRowOut:
    return UsageRowOut(
        key=a.key, calls=a.calls, input_tokens=a.input_tokens,
        output_tokens=a.output_tokens, est_cost_usd=a.est_cost_usd,
        cache_read_tokens=a.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens,
    )


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
        rows=[_row(a) for a in aggregates],
    )


@router.get(
    "/usage/summary",
    response_model=UsageSummaryOut,
    summary="Totals, per-model and per-day usage for one time range",
)
async def get_usage_summary(
    since: float | None = Query(
        None, description="Only count calls at/after this epoch-seconds timestamp",
    ),
    usage_store=Depends(get_usage_store),
) -> UsageSummaryOut:
    """One call for a usage dashboard's whole time range.

    Three groupings a client would otherwise fetch separately and then have to
    keep consistent with each other — and which must agree, because they are
    shown side by side.
    """
    totals = await usage_store.aggregate(since=since)
    by_model = await usage_store.aggregate(since=since, group_by="model")
    by_day = await usage_store.aggregate(since=since, group_by="day")

    from yuyutsava.core.model_router import is_priced, load_price_table

    prices = load_price_table()
    unpriced = sorted(
        {a.key for a in by_model if a.key and not is_priced(a.key, prices)}
    )

    blank = UsageRowOut(key="all", calls=0, input_tokens=0, output_tokens=0,
                        est_cost_usd=0.0)
    return UsageSummaryOut(
        since=since,
        totals=_row(totals[0]) if totals else blank,
        by_model=[_row(a) for a in by_model],
        # Oldest first: this is a series to plot, and the store orders every
        # aggregate by cost.
        by_day=sorted((_row(a) for a in by_day), key=lambda r: r.key),
        unpriced_models=unpriced,
    )


@router.get(
    "/usage/sessions",
    response_model=UsageSessionsOut,
    summary="Per-conversation usage, joined to session titles",
)
async def get_usage_sessions(
    since: float | None = Query(
        None, description="Only count calls at/after this epoch-seconds timestamp",
    ),
    limit: int = Query(50, ge=1, le=500),
    usage_store=Depends(get_usage_store),
) -> UsageSessionsOut:
    """What each conversation cost, most recently active first.

    The join happens here rather than in SQL: on SQLite ``sessions`` lives in a
    different database file from ``llm_usage``, so a joined query would work on
    Postgres only — and a report that exists on one backend is worse than one
    that is assembled in two steps.

    Rows with no matching session keep their thread id and an empty title.
    Model calls made outside any conversation land under thread id ``""``,
    reported as "unattributed" rather than dropped.
    """
    totals = await usage_store.thread_totals(since=since, limit=limit)

    from yuyutsava.core.model_router import is_priced, load_price_table

    prices = load_price_table()
    titles = await _session_titles([t.thread_id for t in totals])

    rows: list[UsageSessionOut] = []
    for t in totals:
        title, origin = titles.get(t.thread_id, ("", ""))
        rows.append(UsageSessionOut(
            thread_id=t.thread_id,
            title=title or ("unattributed" if not t.thread_id else ""),
            origin=origin,
            calls=t.calls,
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cache_read_tokens=t.cache_read_tokens,
            est_cost_usd=t.est_cost_usd,
            # One unpriced model makes the whole row's cost an undercount, so
            # the client can label it rather than showing a confident total.
            priced=bool(t.models) and all(is_priced(m, prices) for m in t.models),
            models=list(t.models),
            first_ts=t.first_ts,
            last_ts=t.last_ts,
        ))
    return UsageSessionsOut(since=since, rows=rows)


async def _session_titles(thread_ids: list[str]) -> dict[str, tuple[str, str]]:
    """``thread_id -> (title, origin)`` for the sessions we can find.

    Best-effort: a usage report with bare thread ids is still useful, and the
    session store being unavailable must not turn it into a 500.
    """
    out: dict[str, tuple[str, str]] = {}
    wanted = {t for t in thread_ids if t}
    if not wanted:
        return out
    try:
        from yuyutsava.storage.sessions import get_default_session_store

        store = get_default_session_store()
        for row in await store.list(limit=500):
            thread_id = getattr(row, "thread_id", "") or ""
            if thread_id in wanted:
                out[thread_id] = (
                    getattr(row, "title", "") or "",
                    getattr(row, "origin", "") or "",
                )
    except Exception:  # noqa: BLE001 — titles are a nicety, not the report
        logger.debug("session titles unavailable for usage report", exc_info=True)
    return out
