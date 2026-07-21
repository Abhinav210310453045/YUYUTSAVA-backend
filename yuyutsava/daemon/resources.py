"""Resource-aware execution (Phase 5): monitor + admission control.

Two cooperating pieces:

- :class:`ResourceMonitor` — an asyncio loop (lifecycle like
  ``UnifiedSweeper.run(stop_event)``) that samples CPU / memory / disk via
  psutil every few seconds into a ring buffer, and — while any task is
  running — debounces a :class:`~yuyutsava.daemon.channels.SystemMetricsPayload`
  onto the user channels so clients see live load without polling.
- :class:`AdmissionController` — estimates a task's weight from its Phase-4
  complexity score (and an env-configured set of heavy ``subagent_hint``s)
  and gates heavy tasks behind a semaphore + load check. A loaded system
  defers a heavy task with backoff up to a ceiling, *then runs it anyway*
  (never starve); only a critically full disk fails a task outright.

The Docker sandbox limits (``DockerSettings`` memory/cpus/pids) remain the
per-task hard cage *below* this governor — admission decides when a task may
start, Docker bounds what it can consume once running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import psutil

from yuyutsava.core.config import _env
from yuyutsava.daemon.channels import ChannelEvent, SystemMetricsPayload, TimelinePayload
from yuyutsava.platform.process import run_capture

logger = logging.getLogger("yuyutsava.daemon.resources")

TaskWeight = Literal["light", "heavy"]

# Ring of recent samples served by GET /system/metrics — 120 × 5s ≈ 10 min.
_RING_SIZE = 120


class DiskCriticalError(RuntimeError):
    """Raised by admission when free disk is below the configured floor.

    The orchestrator loop's existing failure path catches it, marks the task
    ``failed`` with this message, and surfaces it on the channels.
    """


@dataclass(frozen=True)
class ResourceSettings:
    """Tunables for the monitor + admission controller."""

    cpu_high_pct: float = 85.0       # loaded when cpu% is at/above this
    mem_min_mb: int = 1024           # loaded when available memory drops below
    disk_min_gb: float = 5.0         # fail heavy tasks when free disk is below
    max_heavy_tasks: int = 1         # concurrent heavy-task slots
    sample_sec: float = 5.0          # monitor loop interval
    defer_max_sec: float = 600.0     # heavy-task deferral ceiling (then run anyway)
    emit_sec: float = 10.0           # SystemMetricsPayload debounce while tasks run
    heavy_complexity: int = 4        # complexity at/above this → heavy
    heavy_hints: frozenset[str] = frozenset()  # subagent_hints that force heavy
    docker_stats: bool = False       # sample `docker stats` into snapshots (opt-in)

    @classmethod
    def from_env(cls) -> "ResourceSettings":
        def _num(name: str, default: float) -> float:
            raw = _env(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        hints_raw = _env("YUYUTSAVA_RES_HEAVY_HINTS")
        hints = frozenset(h.strip() for h in hints_raw.split(",") if h.strip())
        return cls(
            cpu_high_pct=_num("YUYUTSAVA_RES_CPU_HIGH_PCT", 85.0),
            mem_min_mb=int(_num("YUYUTSAVA_RES_MEM_MIN_MB", 1024)),
            disk_min_gb=_num("YUYUTSAVA_RES_DISK_MIN_GB", 5.0),
            max_heavy_tasks=max(1, int(_num("YUYUTSAVA_MAX_HEAVY_TASKS", 1))),
            sample_sec=max(1.0, _num("YUYUTSAVA_RES_SAMPLE_SEC", 5.0)),
            defer_max_sec=max(0.0, _num("YUYUTSAVA_RES_DEFER_MAX_SEC", 600.0)),
            emit_sec=max(1.0, _num("YUYUTSAVA_RES_EMIT_SEC", 10.0)),
            heavy_complexity=int(_num("YUYUTSAVA_RES_HEAVY_COMPLEXITY", 4)),
            heavy_hints=hints,
            docker_stats=_env("YUYUTSAVA_RES_DOCKER_STATS").lower() in ("1", "true", "yes"),
        )


@dataclass(frozen=True)
class ResourceSnapshot:
    """One point-in-time reading of system load."""

    cpu_pct: float
    mem_available_mb: float
    disk_free_gb: float
    ts: float
    # name → {"cpu_pct": …, "mem_pct": …} per running sandbox container,
    # populated only when ResourceSettings.docker_stats is on.
    per_container: dict[str, dict[str, float]] = field(default_factory=dict)

    def describe(self) -> str:
        return (
            f"cpu {self.cpu_pct:.0f}%, mem {self.mem_available_mb:.0f}MB free, "
            f"disk {self.disk_free_gb:.1f}GB free"
        )


def _psutil_sample() -> ResourceSnapshot:
    """Read cpu / mem / disk via psutil. ``cpu_percent(None)`` is the
    non-blocking delta-since-last-call form — the monitor primes it once at
    loop start so the first real sample isn't a meaningless 0.0."""
    return ResourceSnapshot(
        cpu_pct=psutil.cpu_percent(interval=None),
        mem_available_mb=psutil.virtual_memory().available / (1024 * 1024),
        disk_free_gb=psutil.disk_usage(str(Path.home())).free / (1024 ** 3),
        ts=time.time(),
    )


class ResourceMonitor:
    """Periodic load sampler with a ring buffer of recent snapshots.

    Lifecycle mirrors :class:`yuyutsava.storage.sweeper.UnifiedSweeper`:
    ``run(stop_event)`` slots into the daemon's loop set. ``sampler`` is
    injectable so tests drive deterministic load curves without psutil.
    """

    def __init__(
        self,
        settings: ResourceSettings | None = None,
        *,
        sampler: Callable[[], ResourceSnapshot] | None = None,
        event_sink: Callable[[ChannelEvent], Awaitable[None]] | None = None,
        activity_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._settings = settings or ResourceSettings()
        self._sampler = sampler or _psutil_sample
        self._event_sink = event_sink
        # True while any orchestrator task is running — gates the debounced
        # SystemMetricsPayload emission. Assigned after construction in
        # bootstrap (the AdmissionController it probes needs this monitor).
        self.activity_probe = activity_probe
        self._ring: deque[ResourceSnapshot] = deque(maxlen=_RING_SIZE)
        self._last_emit = 0.0

    @property
    def settings(self) -> ResourceSettings:
        return self._settings

    # --- readings ----------------------------------------------------------

    def snapshot(self) -> ResourceSnapshot | None:
        """Most recent sample, or None before the first tick."""
        return self._ring[-1] if self._ring else None

    def ring(self) -> list[ResourceSnapshot]:
        """Recent samples, oldest first."""
        return list(self._ring)

    def loaded(self) -> bool:
        """High CPU or low memory on the latest sample. False with no data —
        admission must not defer tasks just because the monitor hasn't ticked."""
        snap = self.snapshot()
        if snap is None:
            return False
        return (
            snap.cpu_pct >= self._settings.cpu_high_pct
            or snap.mem_available_mb < self._settings.mem_min_mb
        )

    def disk_critical(self) -> bool:
        snap = self.snapshot()
        if snap is None:
            return False
        return snap.disk_free_gb < self._settings.disk_min_gb

    # --- sampling loop -------------------------------------------------------

    async def sample_once(self) -> ResourceSnapshot:
        """Take one sample, append it to the ring, and return it."""
        # Off-loop: psutil reads (disk_usage → os.statvfs) are blocking and would
        # trip blockbuster on the event loop under allow_blocking=False.
        snap = await asyncio.to_thread(self._sampler)
        if self._settings.docker_stats:
            stats = await self._docker_stats()
            if stats:
                import dataclasses as _dc
                snap = _dc.replace(snap, per_container=stats)
        self._ring.append(snap)
        return snap

    async def run(self, stop_event: asyncio.Event) -> None:
        cfg = self._settings
        logger.info(
            "resources: sampling every %.0fs (cpu_high=%.0f%%, mem_min=%dMB, "
            "disk_min=%.0fGB, max_heavy=%d)",
            cfg.sample_sec, cfg.cpu_high_pct, cfg.mem_min_mb,
            cfg.disk_min_gb, cfg.max_heavy_tasks,
        )
        # Prime psutil's cpu_percent delta so the first ring entry is real.
        with contextlib.suppress(Exception):
            self._sampler()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=cfg.sample_sec)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                snap = await self.sample_once()
                await self._maybe_emit(snap)
            except Exception:
                logger.exception("resources: sample tick failed")

    async def _maybe_emit(self, snap: ResourceSnapshot) -> None:
        """Debounced SystemMetricsPayload — only while tasks run (mobile live
        view without polling), at most one every ``emit_sec``."""
        if self._event_sink is None or self.activity_probe is None:
            return
        if not self.activity_probe():
            return
        now = time.time()
        if now - self._last_emit < self._settings.emit_sec:
            return
        self._last_emit = now
        await self._event_sink(ChannelEvent(payload=SystemMetricsPayload(
            cpu_pct=snap.cpu_pct,
            mem_available_mb=snap.mem_available_mb,
            disk_free_gb=snap.disk_free_gb,
            ts=snap.ts,
        )))

    @staticmethod
    async def _docker_stats() -> dict[str, dict[str, float]]:
        """Best-effort one-shot ``docker stats`` for running containers.

        Opt-in (``YUYUTSAVA_RES_DOCKER_STATS=1``) because it forks a docker
        CLI subprocess per sample; any failure (no docker, daemon down,
        timeout) degrades to an empty mapping.
        """
        try:
            out, _, _ = await run_capture(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                timeout=4.0,
            )
        except Exception:
            return {}
        stats: dict[str, dict[str, float]] = {}
        for line in out.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
                name = row.get("Name") or row.get("ID") or "?"
                stats[name] = {
                    "cpu_pct": float(str(row.get("CPUPerc", "0")).rstrip("%") or 0),
                    "mem_pct": float(str(row.get("MemPerc", "0")).rstrip("%") or 0),
                }
            except (ValueError, TypeError):
                continue
        return stats


class AdmissionController:
    """Gates heavy tasks behind a slot semaphore + system-load check.

    ``weight_for`` is honest-coarse v1: a task is heavy when its Phase-4
    complexity score is at/above ``heavy_complexity`` or its
    ``subagent_hint`` is in the configured heavy set (refine later with
    historical durations from the ``tasks`` table). Light tasks pass
    immediately; heavy tasks wait for a semaphore slot and for the system
    to unload, with exponential backoff capped at ``defer_max_sec`` — after
    which they run anyway (a busy machine slows heavy work down, it never
    starves it). Only a critically full disk fails a task, via
    :class:`DiskCriticalError`.
    """

    # Deferral backoff: first re-check after 2s, doubling to 30s between checks.
    _BACKOFF_INITIAL_SEC = 2.0
    _BACKOFF_MAX_SEC = 30.0

    def __init__(
        self,
        monitor: ResourceMonitor,
        settings: ResourceSettings | None = None,
        *,
        registry: object | None = None,    # daemon.task_registry.TaskRegistry
        event_sink: Callable[[ChannelEvent], Awaitable[None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monitor = monitor
        self._settings = settings or monitor.settings
        self._registry = registry
        self._event_sink = event_sink
        # ``sleep`` + ``clock`` are injectable so tests drive deferral math
        # deterministically (a fake sleep advances a fake monotonic clock).
        self._sleep = sleep
        self._clock = clock
        self._heavy_slots = asyncio.Semaphore(self._settings.max_heavy_tasks)
        # task_id → attribution row for GET /system/metrics.
        self._active: dict[str, dict[str, Any]] = {}

    def weight_for(self, task: object) -> TaskWeight:
        """``task`` is an OrchestratorTask (duck-typed: complexity, subagent_hint)."""
        complexity = getattr(task, "complexity", None)
        if complexity is not None and int(complexity) >= self._settings.heavy_complexity:
            return "heavy"
        if getattr(task, "subagent_hint", "") in self._settings.heavy_hints:
            return "heavy"
        return "light"

    def active(self) -> list[dict[str, Any]]:
        """Per-task attribution: what admission currently holds slots for."""
        return [dict(row) for row in self._active.values()]

    @property
    def max_heavy_tasks(self) -> int:
        return self._settings.max_heavy_tasks

    @property
    def heavy_slots_in_use(self) -> int:
        return self._settings.max_heavy_tasks - self._heavy_slots._value  # noqa: SLF001

    @contextlib.asynccontextmanager
    async def slot(self, task: object, *, task_id: str = ""):
        """Hold an execution slot for ``task`` for the duration of the body.

        Light weight: tracked but never gated. Heavy weight: disk check →
        semaphore → load deferral, recording the total hold-back time as
        ``deferred_ms`` on the task's registry row.
        """
        weight = self.weight_for(task)
        deferred_ms = 0
        if weight == "heavy":
            deferred_ms = await self._admit_heavy(task_id)
            if deferred_ms and self._registry is not None and task_id:
                try:
                    await self._registry.set_deferred_ms(task_id, deferred_ms)
                except Exception:
                    logger.exception("resources: set_deferred_ms failed for %s", task_id)
        key = task_id or f"anon-{id(task)}"
        self._active[key] = {
            "task_id": task_id, "weight": weight,
            "deferred_ms": deferred_ms, "since": time.time(),
        }
        try:
            yield
        finally:
            self._active.pop(key, None)
            if weight == "heavy":
                self._heavy_slots.release()

    async def _admit_heavy(self, task_id: str) -> int:
        """Acquire a heavy slot + wait out load. Returns total deferral in ms.

        Raises :class:`DiskCriticalError` (without holding the semaphore)
        when free disk is below the floor.
        """
        snap = self._monitor.snapshot()
        if self._monitor.disk_critical():
            raise DiskCriticalError(
                f"disk critically low ({snap.disk_free_gb:.1f}GB free < "
                f"{self._settings.disk_min_gb:.0f}GB floor) — refusing to start heavy task"
            )
        start = self._clock()
        if self._heavy_slots.locked():
            await self._post_deferred(task_id, "waiting for a heavy-task slot")
        await self._heavy_slots.acquire()
        try:
            backoff = self._BACKOFF_INITIAL_SEC
            deadline = start + self._settings.defer_max_sec
            while self._monitor.loaded():
                now = self._clock()
                if now >= deadline:
                    await self._post_deferred(
                        task_id,
                        f"deferral ceiling ({self._settings.defer_max_sec:.0f}s) "
                        "reached — running anyway",
                    )
                    break
                snap = self._monitor.snapshot()
                await self._post_deferred(
                    task_id,
                    f"system busy ({snap.describe() if snap else 'no sample'})",
                )
                await self._sleep(min(backoff, max(0.0, deadline - now)))
                backoff = min(backoff * 2, self._BACKOFF_MAX_SEC)
        except BaseException:
            self._heavy_slots.release()
            raise
        return int((self._clock() - start) * 1000)

    async def _post_deferred(self, task_id: str, reason: str) -> None:
        line = f"task {task_id or '?'}: deferred — {reason}"
        logger.info("resources: %s", line)
        if self._event_sink is None:
            return
        try:
            await self._event_sink(ChannelEvent(
                payload=TimelinePayload(line=line, cls="event-decision-skipped"),
                task_id=task_id or None,
            ))
        except Exception:
            logger.exception("resources: deferral timeline emit failed")
