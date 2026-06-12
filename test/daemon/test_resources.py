"""ResourceMonitor + AdmissionController (Phase 5) — fake sampler/clock, no psutil load.

Run:  uv run python -m unittest test.daemon.test_resources -v
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from yuyutsava.daemon.resources import (
    AdmissionController,
    DiskCriticalError,
    ResourceMonitor,
    ResourceSettings,
    ResourceSnapshot,
)
from yuyutsava.daemon.triage_loop import OrchestratorTask


def _snap(cpu: float = 10.0, mem: float = 8000.0, disk: float = 100.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_pct=cpu, mem_available_mb=mem, disk_free_gb=disk, ts=time.time(),
    )


def _task(complexity: int = 3, hint: str = "general-purpose") -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="p", event_id="e", topic="t", summary="s",
        instruction="i", subagent_hint=hint, urgency=2, complexity=complexity,
    )


class _ScriptedSampler:
    """Pops snapshots from a script; repeats the last one when exhausted."""

    def __init__(self, *snaps: ResourceSnapshot) -> None:
        self._snaps = list(snaps)

    def __call__(self) -> ResourceSnapshot:
        if len(self._snaps) > 1:
            return self._snaps.pop(0)
        return self._snaps[0]


class _FakeRegistry:
    def __init__(self) -> None:
        self.deferred: list[tuple[str, int]] = []

    async def set_deferred_ms(self, task_id: str, ms: int) -> None:
        self.deferred.append((task_id, ms))


class _EventSink:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, ev) -> None:
        self.events.append(ev)

    def lines(self) -> list[str]:
        return [getattr(e.payload, "line", "") for e in self.events]


class _Clock:
    """Fake monotonic clock advanced by the fake sleep (no real waiting)."""

    def __init__(self, monitor: ResourceMonitor | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._monitor = monitor

    def clock(self) -> float:
        return self.now

    async def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.now += sec
        # Each deferral sleep ends with a fresh monitor tick, as in prod
        # where the monitor loop samples independently.
        if self._monitor is not None:
            await self._monitor.sample_once()


class ResourceSettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            s = ResourceSettings.from_env()
        self.assertEqual(s.cpu_high_pct, 85.0)
        self.assertEqual(s.mem_min_mb, 1024)
        self.assertEqual(s.disk_min_gb, 5.0)
        self.assertEqual(s.max_heavy_tasks, 1)
        self.assertEqual(s.sample_sec, 5.0)
        self.assertEqual(s.defer_max_sec, 600.0)
        self.assertEqual(s.heavy_complexity, 4)
        self.assertEqual(s.heavy_hints, frozenset())
        self.assertFalse(s.docker_stats)

    def test_env_overrides_and_hint_csv(self) -> None:
        env = {
            "YUYUTSAVA_RES_CPU_HIGH_PCT": "70",
            "YUYUTSAVA_RES_MEM_MIN_MB": "2048",
            "YUYUTSAVA_RES_DISK_MIN_GB": "10",
            "YUYUTSAVA_MAX_HEAVY_TASKS": "2",
            "YUYUTSAVA_RES_SAMPLE_SEC": "1",
            "YUYUTSAVA_RES_DEFER_MAX_SEC": "30",
            "YUYUTSAVA_RES_EMIT_SEC": "20",
            "YUYUTSAVA_RES_HEAVY_COMPLEXITY": "5",
            "YUYUTSAVA_RES_HEAVY_HINTS": "general-purpose, face-watcher",
            "YUYUTSAVA_RES_DOCKER_STATS": "1",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            s = ResourceSettings.from_env()
        self.assertEqual(s.cpu_high_pct, 70.0)
        self.assertEqual(s.mem_min_mb, 2048)
        self.assertEqual(s.max_heavy_tasks, 2)
        self.assertEqual(s.defer_max_sec, 30.0)
        self.assertEqual(s.emit_sec, 20.0)
        self.assertEqual(s.heavy_complexity, 5)
        self.assertEqual(s.heavy_hints, frozenset({"general-purpose", "face-watcher"}))
        self.assertTrue(s.docker_stats)

    def test_malformed_values_fall_back(self) -> None:
        env = {"YUYUTSAVA_RES_CPU_HIGH_PCT": "lots", "YUYUTSAVA_MAX_HEAVY_TASKS": "0"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = ResourceSettings.from_env()
        self.assertEqual(s.cpu_high_pct, 85.0)
        self.assertEqual(s.max_heavy_tasks, 1)  # clamped to ≥1


class ResourceMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_ring_and_load_flags(self) -> None:
        monitor = ResourceMonitor(
            ResourceSettings(),
            sampler=_ScriptedSampler(_snap(cpu=10), _snap(cpu=93), _snap(mem=512), _snap(disk=2)),
        )
        self.assertIsNone(monitor.snapshot())
        self.assertFalse(monitor.loaded())        # no data → never defer
        self.assertFalse(monitor.disk_critical())

        await monitor.sample_once()
        self.assertFalse(monitor.loaded())
        await monitor.sample_once()                # cpu 93 ≥ 85
        self.assertTrue(monitor.loaded())
        await monitor.sample_once()                # mem 512 < 1024
        self.assertTrue(monitor.loaded())
        self.assertFalse(monitor.disk_critical())
        await monitor.sample_once()                # disk 2 < 5
        self.assertTrue(monitor.disk_critical())

        ring = monitor.ring()
        self.assertEqual(len(ring), 4)
        self.assertEqual(ring[0].cpu_pct, 10)      # oldest first
        self.assertEqual(monitor.snapshot(), ring[-1])

    async def test_psutil_sampler_returns_sane_values(self) -> None:
        snap = await ResourceMonitor(ResourceSettings()).sample_once()
        self.assertGreaterEqual(snap.cpu_pct, 0.0)
        self.assertLessEqual(snap.cpu_pct, 100.0 * 2)  # multi-core psutil can exceed 100 briefly
        self.assertGreater(snap.mem_available_mb, 0.0)
        self.assertGreater(snap.disk_free_gb, 0.0)
        self.assertGreater(snap.ts, 0.0)

    async def test_metrics_emission_debounced_and_activity_gated(self) -> None:
        sink = _EventSink()
        monitor = ResourceMonitor(
            ResourceSettings(emit_sec=10.0),
            sampler=_ScriptedSampler(_snap()),
            event_sink=sink,
        )
        active = True
        monitor.activity_probe = lambda: active

        snap = await monitor.sample_once()
        await monitor._maybe_emit(snap)
        await monitor._maybe_emit(snap)            # within emit_sec → debounced
        self.assertEqual(len(sink.events), 1)
        self.assertEqual(sink.events[0].payload.kind, "system_metrics")
        self.assertEqual(sink.events[0].payload.cpu_pct, snap.cpu_pct)

        active = False
        monitor._last_emit = 0.0                   # debounce window cleared
        await monitor._maybe_emit(snap)            # idle daemon → no emission
        self.assertEqual(len(sink.events), 1)


class AdmissionControllerTests(unittest.IsolatedAsyncioTestCase):
    def _admission(
        self,
        *snaps: ResourceSnapshot,
        settings: ResourceSettings | None = None,
        registry: _FakeRegistry | None = None,
    ) -> tuple[AdmissionController, _EventSink, _Clock, ResourceMonitor]:
        settings = settings or ResourceSettings()
        monitor = ResourceMonitor(settings, sampler=_ScriptedSampler(*snaps))
        sink = _EventSink()
        clock = _Clock(monitor)
        adm = AdmissionController(
            monitor, settings, registry=registry, event_sink=sink,
            sleep=clock.sleep, clock=clock.clock,
        )
        return adm, sink, clock, monitor

    def test_weight_matrix(self) -> None:
        adm, *_ = self._admission(_snap())
        self.assertEqual(adm.weight_for(_task(complexity=4)), "heavy")
        self.assertEqual(adm.weight_for(_task(complexity=5)), "heavy")
        self.assertEqual(adm.weight_for(_task(complexity=3)), "light")
        self.assertEqual(adm.weight_for(_task(complexity=1)), "light")
        # No complexity at all → hint is the only signal.
        bare = SimpleNamespace(complexity=None, subagent_hint="x")
        self.assertEqual(adm.weight_for(bare), "light")

    def test_heavy_hint_set(self) -> None:
        adm, *_ = self._admission(
            _snap(), settings=ResourceSettings(heavy_hints=frozenset({"face-watcher"})),
        )
        self.assertEqual(adm.weight_for(_task(complexity=2, hint="face-watcher")), "heavy")
        self.assertEqual(adm.weight_for(_task(complexity=2, hint="other")), "light")

    async def test_light_passes_immediately_even_when_loaded(self) -> None:
        adm, sink, clock, monitor = self._admission(_snap(cpu=99))
        await monitor.sample_once()
        ran = False
        async with adm.slot(_task(complexity=2), task_id="tsk_l"):
            ran = True
            self.assertEqual(adm.active()[0]["weight"], "light")
        self.assertTrue(ran)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(sink.events, [])
        self.assertEqual(adm.active(), [])

    async def test_heavy_defers_under_load_then_runs(self) -> None:
        registry = _FakeRegistry()
        adm, sink, clock, monitor = self._admission(
            _snap(cpu=93), _snap(cpu=95), _snap(cpu=20),
            registry=registry,
        )
        await monitor.sample_once()                # ring: loaded
        ran = False
        async with adm.slot(_task(complexity=5), task_id="tsk_h"):
            ran = True
            row = adm.active()[0]
            self.assertEqual(row["weight"], "heavy")
            self.assertGreater(row["deferred_ms"], 0)
        self.assertTrue(ran)
        # Two loaded samples → two backoff sleeps (2s then 4s) → 6000ms.
        self.assertEqual(clock.sleeps, [2.0, 4.0])
        self.assertEqual(registry.deferred, [("tsk_h", 6000)])
        self.assertTrue(any("system busy" in ln for ln in sink.lines()))
        self.assertEqual(adm.heavy_slots_in_use, 0)

    async def test_heavy_runs_anyway_at_deferral_ceiling(self) -> None:
        registry = _FakeRegistry()
        adm, sink, clock, monitor = self._admission(
            _snap(cpu=99),
            settings=ResourceSettings(defer_max_sec=5.0),
            registry=registry,
        )
        await monitor.sample_once()                # permanently loaded
        ran = False
        async with adm.slot(_task(complexity=5), task_id="tsk_c"):
            ran = True
        self.assertTrue(ran)                       # never starved
        self.assertEqual(sum(clock.sleeps), 5.0)   # waited exactly the ceiling
        self.assertEqual(registry.deferred, [("tsk_c", 5000)])
        self.assertTrue(any("running anyway" in ln for ln in sink.lines()))

    async def test_semaphore_caps_concurrent_heavies(self) -> None:
        adm, sink, _clock, monitor = self._admission(_snap(cpu=10))
        await monitor.sample_once()
        first_in = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def hold_first() -> None:
            async with adm.slot(_task(complexity=5), task_id="tsk_1"):
                order.append("first")
                first_in.set()
                await release_first.wait()

        async def run_second() -> None:
            await first_in.wait()
            async with adm.slot(_task(complexity=5), task_id="tsk_2"):
                order.append("second")

        t1 = asyncio.create_task(hold_first())
        t2 = asyncio.create_task(run_second())
        await first_in.wait()
        await asyncio.sleep(0.05)                  # give second a chance to (wrongly) enter
        self.assertEqual(order, ["first"])
        self.assertEqual(adm.heavy_slots_in_use, 1)
        release_first.set()
        await asyncio.gather(t1, t2)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(adm.heavy_slots_in_use, 0)
        self.assertTrue(any("waiting for a heavy-task slot" in ln for ln in sink.lines()))

    async def test_disk_critical_fails_heavy_task(self) -> None:
        adm, _sink, _clock, monitor = self._admission(_snap(disk=1.0))
        await monitor.sample_once()
        with self.assertRaises(DiskCriticalError) as ctx:
            async with adm.slot(_task(complexity=5), task_id="tsk_d"):
                self.fail("heavy task must not run on a critically full disk")
        self.assertIn("disk critically low", str(ctx.exception))
        self.assertEqual(adm.heavy_slots_in_use, 0)  # nothing leaked
        self.assertEqual(adm.active(), [])

    async def test_unloaded_heavy_passes_without_deferral(self) -> None:
        registry = _FakeRegistry()
        adm, sink, clock, monitor = self._admission(_snap(cpu=10), registry=registry)
        await monitor.sample_once()
        async with adm.slot(_task(complexity=5), task_id="tsk_ok"):
            pass
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(registry.deferred, [])    # ~0ms is not worth a row
        self.assertEqual(sink.events, [])


if __name__ == "__main__":
    unittest.main()
