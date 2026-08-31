"""Pydantic schemas for the system-metrics endpoint (Phase 5)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SnapshotOut(BaseModel):
    cpu_pct: float
    mem_available_mb: float
    disk_free_gb: float
    ts: float
    per_container: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Per-sandbox-container cpu/mem percentages; populated "
                    "only when YUYUTSAVA_RES_DOCKER_STATS=1",
    )


class ActiveTaskOut(BaseModel):
    task_id: str
    weight: str = Field(description='"light" or "heavy"')
    deferred_ms: int = Field(description="How long admission held the task back")
    since: float = Field(description="Epoch-seconds when the slot was granted")


class HeavySlotsOut(BaseModel):
    max: int
    in_use: int


class SystemMetricsOut(BaseModel):
    current: SnapshotOut | None = Field(
        None, description="Latest sample; null before the monitor's first tick",
    )
    loaded: bool = Field(description="High CPU or low memory on the latest sample")
    disk_critical: bool = Field(description="Free disk below the heavy-task floor")
    ring: list[SnapshotOut] = Field(
        description="Recent samples, oldest first (~10 minutes at 5s cadence)",
    )
    heavy_slots: HeavySlotsOut | None = Field(
        None, description="Null when no admission controller is wired",
    )
    active_tasks: list[ActiveTaskOut] = Field(
        default_factory=list,
        description="Per-task attribution: slots admission currently holds",
    )
