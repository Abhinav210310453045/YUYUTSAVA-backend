"""System load metrics (Phase 5 resource governor).

One endpoint over the :class:`~yuyutsava.daemon.resources.ResourceMonitor`
ring buffer plus the admission controller's per-task attribution — the
mobile dashboard's data source (the live push variant is the debounced
``SystemMetricsPayload`` on the SSE stream).
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends

from yuyutsava.daemon.web.deps import get_admission_controller, get_resource_monitor
from yuyutsava.daemon.web.schemas.system import (
    ActiveTaskOut,
    HeavySlotsOut,
    SnapshotOut,
    SystemMetricsOut,
)

router = APIRouter(tags=["system"])


def _snapshot_out(snap) -> SnapshotOut:
    return SnapshotOut(**dataclasses.asdict(snap))


@router.get(
    "/system/metrics",
    response_model=SystemMetricsOut,
    summary="Current system load, recent history, and running-task attribution",
)
async def get_system_metrics(
    monitor=Depends(get_resource_monitor),
    admission=Depends(get_admission_controller),
) -> SystemMetricsOut:
    current = monitor.snapshot()
    heavy_slots = None
    active: list[ActiveTaskOut] = []
    if admission is not None:
        heavy_slots = HeavySlotsOut(
            max=admission.max_heavy_tasks,
            in_use=admission.heavy_slots_in_use,
        )
        active = [ActiveTaskOut(**row) for row in admission.active()]
    return SystemMetricsOut(
        current=_snapshot_out(current) if current is not None else None,
        loaded=monitor.loaded(),
        disk_critical=monitor.disk_critical(),
        ring=[_snapshot_out(s) for s in monitor.ring()],
        heavy_slots=heavy_slots,
        active_tasks=active,
    )
