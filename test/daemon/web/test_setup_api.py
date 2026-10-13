"""``/setup``: the app asks the same questions the terminal wizard asks.

What must hold:

* the state the screen draws from costs no probe — a screen that cannot appear
  until a Postgres socket times out is worse than one saying "not checked";
* nothing is started, spent or written except by a request that asks for it;
* a value never travels back out, secret or not;
* the services it offers are the ones the compose files define, and the probes
  are the ones the catalog names, so this and the terminal cannot disagree.

Run:  .venv/bin/python -m pytest test/daemon/web/test_setup_api.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from yuyutsava.config.checks import Check
from yuyutsava.config.source import env_source, settings_source
from yuyutsava.daemon.web.exceptions import register_exception_handlers
from yuyutsava.daemon.web.routers import setup as setup_router
from yuyutsava.daemon.web.services.setup_service import SetupService


class _Containers:
    """A container service that answers as told and records what it was asked."""

    def __init__(self, *, engine_ok: bool = True) -> None:
        self.engine_ok = engine_ok
        self.started: list[str] = []
        self.launched = 0

    def engine(self) -> Check:
        if self.engine_ok:
            return Check("Docker", note="Docker Engine 29.6.1")
        return Check("Docker", problem="not responding", fix="Start Docker Desktop.")

    def engine_launch_command(self) -> str:
        return "Docker Desktop.exe"

    def launch_engine(self) -> Check:
        self.launched += 1
        self.engine_ok = True
        return self.engine()

    def start(self, service: str) -> Check:
        self.started.append(service)
        return Check(service, note="healthy")

    def command(self, service: str) -> str:
        return f"docker compose up -d {service}"

    def purpose(self, service: str) -> str:
        return "does something useful"


class _Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        for patch in (
            mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)}, clear=True),
            mock.patch(
                "yuyutsava.config.env_file.project_env_path", return_value=self.home / "p.env"
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.containers = _Containers()
        self.service = SetupService(self.containers)
        app = FastAPI()
        app.include_router(setup_router.router)
        register_exception_handlers(app)
        # The router holds one service; this test's has doubles for Docker.
        service_patch = mock.patch.object(setup_router, "_service", self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()


class TheState(_Api):
    async def test_it_draws_without_running_a_single_probe(self) -> None:
        with mock.patch(
            "yuyutsava.config.checks.run", side_effect=AssertionError("no probe may run")
        ):
            answer = await self.client.get("/setup/state")
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        self.assertIn("source", body)
        self.assertIn("services", body)

    async def test_it_says_which_file_is_in_force_and_what_else_there_is(self) -> None:
        body = (await self.client.get("/setup/state")).json()
        self.assertEqual(body["source"]["active"]["kind"], "settings")
        kinds = {choice["kind"] for choice in body["source"]["choices"]}
        self.assertEqual(kinds, {"settings", "env"})

    async def test_it_says_what_is_set_without_saying_what_it_says(self) -> None:
        settings_source().write({"TAVILY_API_KEY": "tvly-super-secret"})
        answer = await self.client.get("/setup/state")
        self.assertNotIn("tvly-super-secret", answer.text, "a value never travels back out")
        state = {one["key"]: one for one in answer.json()["settings"]}
        self.assertTrue(state["TAVILY_API_KEY"]["set"])
        self.assertIn("settings.toml", state["TAVILY_API_KEY"]["source"])
        self.assertFalse(state["EXA_API_KEY"]["set"])

    async def test_the_services_are_the_ones_the_compose_files_define(self) -> None:
        body = (await self.client.get("/setup/state")).json()
        names = {service["name"] for service in body["services"]}
        self.assertEqual(names, {"postgres", "kroki", "langfuse"})
        for service in body["services"]:
            with self.subTest(service=service["name"]):
                self.assertTrue(service["command"], "the app shows the command before running it")

    async def test_it_says_which_settings_each_service_is_needed_by(self) -> None:
        body = (await self.client.get("/setup/state")).json()
        by_name = {service["name"]: service for service in body["services"]}
        self.assertIn("YUYUTSAVA_STORAGE_BACKEND", by_name["postgres"]["needed_by"])
        self.assertIn("YUYUTSAVA_KROKI_URL", by_name["kroki"]["needed_by"])

    async def test_a_stopped_engine_is_reported_with_how_to_start_it(self) -> None:
        self.containers.engine_ok = False
        body = (await self.client.get("/setup/state")).json()
        self.assertFalse(body["engine"]["ok"])
        self.assertTrue(body["engine_launch_command"])


class Checking(_Api):
    async def test_a_probe_runs_when_asked_for(self) -> None:
        with mock.patch("yuyutsava.config.checks.run", return_value=Check("Kroki")) as probe:
            answer = await self.client.post("/setup/check/kroki")
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.json()["ok"])
        probe.assert_called_once()

    async def test_nothing_is_spent_unless_the_request_says_so(self) -> None:
        with mock.patch("yuyutsava.config.checks.run", return_value=Check("Tavily")) as probe:
            await self.client.post("/setup/check/tavily")
            self.assertEqual(probe.call_args.kwargs, {}, "a credit was spent unasked")
            await self.client.post("/setup/check/tavily", json={"live": True})
            self.assertEqual(probe.call_args.kwargs, {"live": True})

    async def test_live_means_nothing_for_a_probe_that_costs_nothing(self) -> None:
        with mock.patch("yuyutsava.config.checks.run", return_value=Check("Kroki")) as probe:
            await self.client.post("/setup/check/kroki", json={"live": True})
        self.assertEqual(probe.call_args.kwargs, {})

    async def test_a_probe_nobody_has_heard_of_is_a_404(self) -> None:
        answer = await self.client.post("/setup/check/nothing-like-this")
        self.assertEqual(answer.status_code, 404)


class Starting(_Api):
    async def test_a_container_starts_only_on_a_request_that_asks(self) -> None:
        self.assertEqual(self.containers.started, [], "drawing the screen started nothing")
        answer = await self.client.post("/setup/services/postgres/start")
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.json()["ok"])
        self.assertEqual(self.containers.started, ["postgres"])

    async def test_something_it_cannot_start_is_a_404(self) -> None:
        answer = await self.client.post("/setup/services/nothing-like-this/start")
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(self.containers.started, [])

    async def test_the_engine_starts_only_on_a_request_that_asks(self) -> None:
        self.containers.engine_ok = False
        self.assertEqual(self.containers.launched, 0)
        answer = await self.client.post("/setup/engine/start")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.containers.launched, 1)


class Writing(_Api):
    async def test_settings_go_to_whichever_file_is_in_force(self) -> None:
        answer = await self.client.put(
            "/setup/settings", json={"values": {"YUYUTSAVA_KROKI_URL": "http://here:8000"}}
        )
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["written"], ["YUYUTSAVA_KROKI_URL"])
        self.assertEqual(settings_source().values()["YUYUTSAVA_KROKI_URL"], "http://here:8000")

    async def test_an_empty_value_removes_a_setting(self) -> None:
        settings_source().write({"TAVILY_API_KEY": "t"})
        await self.client.put("/setup/settings", json={"values": {"TAVILY_API_KEY": ""}})
        self.assertNotIn("TAVILY_API_KEY", settings_source().values())

    async def test_a_name_the_catalog_does_not_know_is_refused(self) -> None:
        answer = await self.client.put("/setup/settings", json={"values": {"NOT_A_SETTING": "x"}})
        self.assertEqual(answer.status_code, 422)
        self.assertFalse(settings_source().exists, "nothing was written")


class MovingBetweenFiles(_Api):
    async def test_a_move_carries_every_setting_across(self) -> None:
        env_source().write({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"})
        answer = await self.client.put("/setup/source", json={"kind": "settings"})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["moved"], ["EXA_API_KEY", "TAVILY_API_KEY"])
        self.assertEqual(env_source().values(), {})
        self.assertEqual(answer.json()["source"]["active"]["kind"], "settings")

    async def test_somewhere_else_is_refused(self) -> None:
        answer = await self.client.put("/setup/source", json={"kind": "somewhere-else"})
        self.assertEqual(answer.status_code, 422)

    async def test_the_source_can_be_read_on_its_own(self) -> None:
        answer = await self.client.get("/setup/source")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("active", answer.json())


if __name__ == "__main__":
    unittest.main()
