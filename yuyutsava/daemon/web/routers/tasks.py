"""First-class task endpoints: submit, list, status, cancel, event replay.

The submission service handles trust routing (direct vs triage); this
router is a thin HTTP veneer over it and the TaskRegistry. Event replay is
served from the WebHub's per-task ring buffer (last 500 stream items), so a
client that reconnects mid-task calls ``GET /tasks/{id}/events`` to fill
the gap and then resumes ``/stream?task_id=``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from yuyutsava.daemon.task_registry import TASK_STATUSES
from yuyutsava.daemon.web.deps import get_hub, get_task_registry, get_task_submission
from yuyutsava.daemon.web.exceptions import ConflictError, NotFoundError
from yuyutsava.daemon.web.schemas.proposal import OkOut
from yuyutsava.daemon.web.schemas.task import (
    TaskEventsOut, TaskListOut, TaskOut, TaskSubmitIn, TaskSubmitOut,
)

router = APIRouter(tags=["tasks"])


def _to_out(rec) -> TaskOut:
    return TaskOut(**rec.as_dict())


@router.post(
    "/tasks",
    response_model=TaskSubmitOut,
    summary="Submit a task (direct = run now, triage = consent flow)",
)
async def submit_task(
    body: TaskSubmitIn,
    submission=Depends(get_task_submission),
) -> TaskSubmitOut:
    if body.mode == "direct":
        task_id = await submission.submit_direct(body.instruction, origin=body.origin)
    else:
        task_id = await submission.submit_via_triage(body.instruction, origin=body.origin)
    return TaskSubmitOut(task_id=task_id, mode=body.mode)


@router.get(
    "/tasks",
    response_model=TaskListOut,
    summary="List tasks newest-first (cursor pagination)",
)
async def list_tasks(
    status: str | None = Query(None, description=f"Filter: one of {TASK_STATUSES}"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="task_id from the previous page's next_cursor"),
    registry=Depends(get_task_registry),
) -> TaskListOut:
    if status is not None and status not in TASK_STATUSES:
        raise NotFoundError(f"unknown status {status!r}")
    records, next_cursor = await registry.list(status=status, limit=limit, cursor=cursor)
    return TaskListOut(tasks=[_to_out(r) for r in records], next_cursor=next_cursor)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskOut,
    summary="Status, timings, and result of one task",
)
async def get_task(task_id: str, registry=Depends(get_task_registry)) -> TaskOut:
    rec = await registry.get(task_id)
    if rec is None:
        raise NotFoundError(f"no task {task_id!r}")
    return _to_out(rec)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=OkOut,
    summary="Request cancellation (honored between stream events — coarse v1)",
)
async def cancel_task(task_id: str, registry=Depends(get_task_registry)) -> OkOut:
    outcome = await registry.request_cancel(task_id)
    if outcome == "not_found":
        raise NotFoundError(f"no task {task_id!r}")
    if outcome == "conflict":
        raise ConflictError("task already finished")
    return OkOut(ok=True, note="cancellation requested; honored at the next stream event")


@router.get(
    "/tasks/{task_id}/events",
    response_model=TaskEventsOut,
    summary="Replay the task's recent stream items (ring buffer, last 500)",
)
async def task_events(
    task_id: str,
    registry=Depends(get_task_registry),
    hub=Depends(get_hub),
) -> TaskEventsOut:
    rec = await registry.get(task_id)
    if rec is None:
        raise NotFoundError(f"no task {task_id!r}")
    items = hub.task_events(task_id)
    return TaskEventsOut(task_id=task_id, events=[it.to_wire_dict() for it in items])
