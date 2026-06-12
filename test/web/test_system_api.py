"""GET /system/metrics: monitor snapshot + ring + admission attribution.

Run:  uv run python -m unittest test.web.test_system_api -v
"""

from __future__ import annotations

import time
import unittest

import httpx

from yuyutsava.daemon.resources import (
    AdmissionController,
    ResourceMonitor,
    ResourceSettings,
    ResourceSnapshot,
)
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


def _snap(cpu: float = 12.5, mem: float = 4096.0, disk: float = 42.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_pct=cpu, mem_available_mb=mem, disk_free_gb=disk, ts=time.time(),
    )


def _task(complexity: int = 5) -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="p", event_id="e", topic="t", summary="s",
        instruction="i", subagent_hint="general-purpose", urgency=2,
        complexity=complexity,
    )


class SystemApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = ResourceSettings(max_heavy_tasks=2)
        self.monitor = ResourceMonitor(self.settings, sampler=_snap)
        self.admission = AdmissionController(self.monitor, self.settings)
        app = create_app(
            WebHub(store=_RecordingStore()), host="127.0.0.1",
            resource_monitor=self.monitor,
            admission_controller=self.admission,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_metrics_before_first_sample(self) -> None:
        r = await self.client.get("/system/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["current"])
        self.assertEqual(body["ring"], [])
        self.assertFalse(body["loaded"])
        self.assertFalse(body["disk_critical"])
        self.assertEqual(body["heavy_slots"], {"max": 2, "in_use": 0})
        self.assertEqual(body["active_tasks"], [])

    async def test_metrics_with_samples_and_active_task(self) -> None:
        await self.monitor.sample_once()
        await self.monitor.sample_once()
        async with self.admission.slot(_task(), task_id="tsk_x"):
            r = await self.client.get("/system/metrics")
            body = r.json()
            self.assertAlmostEqual(body["current"]["cpu_pct"], 12.5)
            self.assertAlmostEqual(body["current"]["disk_free_gb"], 42.0)
            self.assertEqual(len(body["ring"]), 2)
            self.assertEqual(body["heavy_slots"], {"max": 2, "in_use": 1})
            self.assertEqual(len(body["active_tasks"]), 1)
            self.assertEqual(body["active_tasks"][0]["task_id"], "tsk_x")
            self.assertEqual(body["active_tasks"][0]["weight"], "heavy")
        r = await self.client.get("/system/metrics")
        self.assertEqual(r.json()["active_tasks"], [])

    async def test_no_admission_degrades_to_monitor_only(self) -> None:
        app = create_app(
            WebHub(store=_RecordingStore()), host="127.0.0.1",
            resource_monitor=self.monitor,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/system/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["heavy_slots"])
        self.assertEqual(r.json()["active_tasks"], [])

    async def test_missing_monitor_is_503(self) -> None:
        app = create_app(WebHub(store=_RecordingStore()), host="127.0.0.1")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/system/metrics")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
