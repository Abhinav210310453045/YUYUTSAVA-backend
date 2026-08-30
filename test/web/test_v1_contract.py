"""Phase 6 contract tests: every /v1 endpoint answers, legacy unprefixed
aliases answer identically, and the OpenAPI schema documents only /v1.

Run:  uv run python -m unittest test.web.test_v1_contract -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from yuyutsava.daemon.resources import ResourceSnapshot
from yuyutsava.daemon.task_registry import TaskRegistry
from yuyutsava.daemon.task_store_unified import sqlite_task_store
from yuyutsava.daemon.task_submission import TaskSubmissionService
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.auth import AuthSettings, check_request
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.storage.models import ConsentRule, Decision
from yuyutsava.storage.sessions.sqlite_impl import SqliteSessionStore


# --------------------------------------------------------------------------
# Fakes — duck-typed singletons create_app attaches to app.state
# --------------------------------------------------------------------------


_DECISIONS = [
    Decision(decision_id=f"dec_{i}", proposal_id=None, event_id=f"evt_{i}",
             outcome="orchestrator_done", action_summary=f"did thing {i}",
             ts=float(100 + i))
    for i in (3, 2, 1)  # newest first, ts 103/102/101
]

_RULES = [ConsentRule(rule_id="rul_1", topic_glob="fs.*", match_json="{}",
                      decision="auto_approve", created_ts=50.0, expires_ts=None)]


class _EventsStore:
    """The slice of the events Store the wired routers/services touch."""

    async def list_decisions(self, limit: int = 50, cursor: float | None = None):
        rows = [d for d in _DECISIONS if cursor is None or d.ts < cursor]
        return rows[:limit]

    async def list_consent_rules(self):
        return list(_RULES)

    async def try_set_proposal_status(self, proposal_id, *, from_status, to_status):
        return True

    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


class _Bus:
    async def publish(self, ev) -> None: ...


_SNAP = ResourceSnapshot(cpu_pct=12.5, mem_available_mb=4096.0,
                         disk_free_gb=200.0, ts=1000.0)


class _Monitor:
    def snapshot(self):
        return _SNAP

    def ring(self):
        return [_SNAP]

    def loaded(self) -> bool:
        return False

    def disk_critical(self) -> bool:
        return False


class _UsageStore:
    async def aggregate(self, since=None, group_by=None):
        return [SimpleNamespace(key="all", calls=2, input_tokens=100,
                                output_tokens=20, est_cost_usd=0.003)]


class _ChannelPlugins:
    """Registry double for snapshot + hot enable/disable (config persistence
    is a no-op; the real file path is covered in test_channels_api)."""

    def __init__(self) -> None:
        self._running: set[str] = set()
        self.config = SimpleNamespace(
            with_enabled=lambda name, en: SimpleNamespace(to_file=lambda: None),
        )

    def set_config(self, cfg) -> None: ...

    def snapshot(self):
        return [{
            "name": "telegram",
            "available": True,
            "enabled": "telegram" in self._running,
            "running": "telegram" in self._running,
            "capabilities": ["notify", "proposal", "ask", "invoke"],
        }]

    async def enable(self, name: str) -> bool:
        if name != "telegram":
            raise KeyError(name)
        changed = name not in self._running
        self._running.add(name)
        return changed

    async def disable(self, name: str) -> bool:
        changed = name in self._running
        self._running.discard(name)
        return changed


class V1ContractTests(unittest.IsolatedAsyncioTestCase):
    """Golden tests for the frozen /v1 mobile contract."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        self.registry = TaskRegistry(sqlite_task_store(tmp / "state.db"))
        self.queue: asyncio.Queue = asyncio.Queue()
        self.store = _EventsStore()
        self.hub = WebHub(store=self.store)
        submission = TaskSubmissionService(
            registry=self.registry, task_queue=self.queue,
            store=self.store, bus=_Bus(), proposal_expiry_sec=300,
        )
        self.sessions = SqliteSessionStore(tmp / "sessions.db")
        self._sessions_patch = mock.patch(
            "yuyutsava.daemon.web.routers.sessions.get_default_session_store",
            return_value=self.sessions,
        )
        self._sessions_patch.start()

        app = create_app(
            self.hub, host="127.0.0.1",
            task_registry=self.registry, task_submission=submission,
            channel_plugins=_ChannelPlugins(),
            usage_store=_UsageStore(),
            resource_monitor=_Monitor(),
            admission_controller=SimpleNamespace(
                max_heavy_tasks=1, heavy_slots_in_use=0, active=lambda: [],
            ),
            model_router=SimpleNamespace(enabled=True),
            memory_store=object(),
            async_subagents=True,
        )
        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._sessions_patch.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    # Individual /v1 endpoints                                            #
    # ------------------------------------------------------------------ #

    async def test_health(self) -> None:
        r = await self.client.get("/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    async def test_server_info_capabilities(self) -> None:
        r = await self.client.get("/v1/server-info")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["name"], "yuyutsava")
        self.assertEqual(body["api_version"], "v1")
        self.assertTrue(body["version"])
        self.assertEqual(body["capabilities"], {
            "model_routing": True, "memory": True,
            "resource_governor": True, "async_subagents": True,
        })
        self.assertEqual(body["channels"][0]["name"], "telegram")

    async def test_server_info_degrades_when_nothing_wired(self) -> None:
        bare = create_app(WebHub(store=_EventsStore()), host="127.0.0.1")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bare), base_url="http://test",
        ) as client:
            body = (await client.get("/v1/server-info")).json()
        self.assertEqual(body["capabilities"], {
            "model_routing": False, "memory": False,
            "resource_governor": False, "async_subagents": False,
        })
        self.assertEqual(body["channels"], [])

    async def test_tasks_lifecycle(self) -> None:
        r = await self.client.post(
            "/v1/tasks", json={"instruction": "do it", "mode": "direct"},
        )
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self.assertTrue(task_id.startswith("tsk_"))

        r = await self.client.get("/v1/tasks", params={"limit": 10})
        body = r.json()
        self.assertEqual(body["tasks"][0]["task_id"], task_id)
        self.assertIn("next_cursor", body)

        r = await self.client.get(f"/v1/tasks/{task_id}")
        self.assertEqual(r.json()["status"], "queued")

        r = await self.client.get(f"/v1/tasks/{task_id}/events")
        self.assertEqual(r.json(), {"task_id": task_id, "events": []})

        r = await self.client.post(f"/v1/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    async def test_decisions_pagination(self) -> None:
        r = await self.client.get("/v1/decisions", params={"limit": 2})
        page1 = r.json()
        self.assertEqual([d["ts"] for d in page1], [103.0, 102.0])

        r = await self.client.get(
            "/v1/decisions", params={"limit": 2, "cursor": page1[-1]["ts"]},
        )
        page2 = r.json()
        self.assertEqual([d["ts"] for d in page2], [101.0])

    async def test_sessions_pagination(self) -> None:
        ws = Path(self._tmp.name)
        for i in range(3):
            await self.sessions.create(workspace=ws, task=f"task {i}",
                                       thread_id=f"thr_{i}")
            await asyncio.sleep(0.01)  # distinct updated_at for keyset paging

        r = await self.client.get("/v1/sessions", params={"limit": 2})
        page1 = r.json()
        self.assertEqual(len(page1), 2)
        self.assertEqual(page1[0]["id"], "thr_2")  # newest first

        r = await self.client.get(
            "/v1/sessions",
            params={"limit": 2, "cursor": page1[-1]["updated_at"]},
        )
        page2 = r.json()
        self.assertEqual([s["id"] for s in page2], ["thr_0"])

        r = await self.client.get("/v1/sessions/thr_1")
        self.assertEqual(r.json()["task_preview"], "task 1")

    async def test_proposal_and_ask_respond(self) -> None:
        loop = asyncio.get_running_loop()
        fut_p = loop.create_future()
        self.hub.pending_proposals["pp_1"] = fut_p
        r = await self.client.post(
            "/v1/proposal/pp_1/respond", json={"decision": "approve"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(fut_p.result().decision, "approve")

        fut_a = loop.create_future()
        self.hub.pending_asks["aa_1"] = fut_a
        r = await self.client.post(
            "/v1/ask/aa_1/respond", json={"response": "yes"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fut_a.result(), "yes")

        # Unknown ask → 409 conflict (the wire contract for "gone").
        r = await self.client.post(
            "/v1/ask/aa_missing/respond", json={"response": "yes"},
        )
        self.assertEqual(r.status_code, 409)

    async def test_rules_skills_usage_system_channels(self) -> None:
        r = await self.client.get("/v1/rules")
        self.assertEqual(r.json()[0]["rule_id"], "rul_1")

        r = await self.client.get("/v1/skills")
        self.assertEqual(r.json(), [])  # no registry wired → empty list

        r = await self.client.get("/v1/usage")
        self.assertEqual(r.json()["rows"][0]["key"], "all")

        r = await self.client.get("/v1/system/metrics")
        body = r.json()
        self.assertEqual(body["current"]["cpu_pct"], 12.5)
        self.assertEqual(body["heavy_slots"], {"max": 1, "in_use": 0})

        r = await self.client.get("/v1/channels")
        self.assertEqual(r.json()["channels"][0]["name"], "telegram")

    async def test_channel_enable_disable(self) -> None:
        r = await self.client.post("/v1/channels/telegram/enable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["changed"])

        r = await self.client.post("/v1/channels/telegram/disable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["changed"])

        r = await self.client.post("/v1/channels/bogus/enable")
        self.assertEqual(r.status_code, 404)

    async def test_logs_level_and_config_events(self) -> None:
        r = await self.client.get("/v1/logs/level")
        self.assertIn(r.json()["level"], ("DEBUG", "INFO", "WARNING"))

        r = await self.client.get("/v1/config/events")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sources", r.json())

    async def test_stream_route_mounted_on_both_prefixes(self) -> None:
        paths = {route.path for route in self.app.routes}
        self.assertIn("/stream", paths)
        self.assertIn("/v1/stream", paths)

    # ------------------------------------------------------------------ #
    # Alias regression: unprefixed legacy paths answer identically        #
    # ------------------------------------------------------------------ #

    async def test_legacy_aliases_answer_identically(self) -> None:
        await self.client.post("/v1/tasks", json={"instruction": "x"})
        for path in (
            "/server-info", "/decisions", "/rules", "/skills", "/tasks",
            "/usage", "/system/metrics", "/channels", "/sessions",
        ):
            with self.subTest(path=path):
                legacy = await self.client.get(path)
                v1 = await self.client.get(f"/v1{path}")
                self.assertEqual(legacy.status_code, 200)
                self.assertEqual(legacy.status_code, v1.status_code)
                self.assertEqual(legacy.json(), v1.json())

    async def test_legacy_health_alive(self) -> None:
        # /health carries a fresh ts per call; compare the stable field.
        legacy = await self.client.get("/health")
        v1 = await self.client.get("/v1/health")
        self.assertEqual(legacy.json()["status"], v1.json()["status"])

    async def test_legacy_post_aliases(self) -> None:
        r = await self.client.post("/tasks", json={"instruction": "legacy"})
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        # Visible through the /v1 surface and vice versa.
        r = await self.client.get(f"/v1/tasks/{task_id}")
        self.assertEqual(r.json()["instruction"], "legacy")

    # ------------------------------------------------------------------ #
    # OpenAPI: only /v1 is documented (mobile TS client generates from it)
    # ------------------------------------------------------------------ #

    async def test_openapi_documents_only_v1(self) -> None:
        schema = (await self.client.get("/openapi.json")).json()
        paths = list(schema["paths"])
        self.assertTrue(paths, "no paths in schema")
        non_v1 = [p for p in paths if not p.startswith("/v1/")]
        self.assertEqual(non_v1, [])
        self.assertIn("/v1/server-info", paths)
        self.assertIn("/v1/tasks", paths)


class V1AuthTests(unittest.TestCase):
    """The bearer rules extend to the /v1 surface."""

    settings = AuthSettings(token="sekret", enforce=True)

    def test_v1_health_public(self) -> None:
        self.assertTrue(check_request(self.settings, path="/v1/health"))

    def test_v1_paths_require_bearer(self) -> None:
        self.assertFalse(check_request(self.settings, path="/v1/tasks"))
        self.assertTrue(check_request(
            self.settings, path="/v1/tasks", authorization="Bearer sekret",
        ))

    def test_query_token_on_both_stream_paths_only(self) -> None:
        self.assertTrue(check_request(
            self.settings, path="/v1/stream", query_token="sekret",
        ))
        self.assertTrue(check_request(
            self.settings, path="/stream", query_token="sekret",
        ))
        self.assertFalse(check_request(
            self.settings, path="/v1/tasks", query_token="sekret",
        ))


if __name__ == "__main__":
    unittest.main()
