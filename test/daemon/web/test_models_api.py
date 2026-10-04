"""``/models``: the app reads models.toml, sets up providers, and changes it — safely.

A real store, source and session store in a temporary folder; provider listing
and detection are doubles, so nothing calls out.

Run:  .venv/bin/python -m pytest test/daemon/web/test_models_api.py
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.services.models_service import ModelsService, broadcast_model_changes
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.llm.config.detect import Finding
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.config.watch import ConfigSource
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.selection import TaskModels
from yuyutsava.storage.sessions import SqliteSessionStore

MODELS = """\
# kept by hand
version = 1
[defaults]
model = "flash"
[providers.work]
type = "vertex"
project = "p"
location = "global"
auth = { method = "adc" }
[models.flash]
provider = "work"
id = "gemini-3.5-flash"
"""


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


def _listing(access) -> Discovery:
    if access.connection.type == "groq" and not access.secrets:
        return Discovery(access.provider, problem="no key", fix="add one")
    return Discovery(
        access.provider,
        models=(DiscoveredModel(id="m1", availability=Availability.AVAILABLE),),
    )


class _Api(unittest.IsolatedAsyncioTestCase):
    write_file = True

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        if self.write_file:
            (self.dir / "models.toml").write_text(MODELS, encoding="utf-8")
        self.source = ConfigSource(self.dir / "models.toml", self.dir / "secrets.toml")
        self.source.reload()
        self.sessions = SqliteSessionStore(self.dir / "sessions.db")
        self.service = ModelsService(
            ConfigStore(self.dir / "models.toml", self.dir / "secrets.toml"),
            self.source,
            discover=_listing,
            detect=lambda: [
                Finding(
                    "groq",
                    "groq",
                    "the GROQ_API_KEY variable",
                    {"type": "groq", "auth": {"method": "api_key", "key": "env:GROQ_API_KEY"}},
                )
            ],
        )
        self.hub = WebHub(store=_RecordingStore())
        app = create_app(
            self.hub,
            host="127.0.0.1",
            deps=AppDeps(
                models_service=self.service,
                session_store=self.sessions,
                task_models=TaskModels(self.source),
            ),
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._tmp.cleanup()

    async def status(self) -> dict:
        r = await self.client.get("/models")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def text(self) -> str:
        return (self.dir / "models.toml").read_text(encoding="utf-8")


class Reading(_Api):
    async def test_status_names_the_default_tasks_providers_and_revision(self) -> None:
        body = await self.status()
        self.assertTrue(body["configured"])
        self.assertEqual(body["default"], "flash")
        self.assertEqual(body["providers"][0]["settings"], {"project": "p", "location": "global"})
        chat = next(t for t in body["tasks"] if t["name"] == "chat")
        self.assertEqual((chat["reference"], chat["follows_default"]), ("flash", True))
        self.assertEqual(len(body["revision"]), 64)

    async def test_the_catalog_has_every_provider_type_and_its_logins(self) -> None:
        catalog = (await self.client.get("/v1/models/catalog")).json()
        vertex = next(f for f in catalog if f["type"] == "vertex")
        self.assertEqual([f["name"] for f in vertex["settings"]], ["project", "location"])
        self.assertIn("adc", [login["method"] for login in vertex["logins"]])

    async def test_detected_logins_carry_references_not_keys(self) -> None:
        found = (await self.client.get("/models/detected")).json()
        self.assertEqual(found[0]["connection"]["auth"]["key"], "env:GROQ_API_KEY")


class SettingUp(_Api):
    async def test_check_keeps_a_typed_key_in_secrets_and_logs_in(self) -> None:
        r = await self.client.post(
            "/models/check",
            json={
                "name": "groq",
                "connection": {"type": "groq", "auth": {"method": "api_key"}},
                "secrets": {"key": "gsk-1"},
            },
        )
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["connection"]["auth"]["key"], "secret:groq")
        self.assertIn('groq = "gsk-1"', (self.dir / "secrets.toml").read_text())
        self.assertNotIn("gsk-1", r.text)
        self.assertNotIn("groq", self.text())  # checked, not saved

    async def test_a_failed_login_says_why(self) -> None:
        r = await self.client.post(
            "/models/check",
            json={
                "name": "groq",
                "connection": {
                    "type": "groq",
                    "auth": {"method": "api_key", "key": "env:NOPE_KEY"},
                },
            },
        )
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["problem"])

    async def test_setup_writes_everything_at_once_and_keeps_comments(self) -> None:
        revision = (await self.status())["revision"]
        r = await self.client.post(
            "/models/setup",
            json={
                "revision": revision,
                "providers": [{"name": "local", "connection": {"type": "ollama"}}],
                "aliases": [{"name": "qwen", "provider": "local", "id": "qwen3:8b"}],
                "default": "qwen",
                "tasks": {"triage": "flash"},
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["default"], "qwen")
        self.assertIn("# kept by hand", self.text())
        self.assertIn('triage = "flash"', self.text())


class Changing(_Api):
    async def test_the_default_and_a_task(self) -> None:
        revision = (await self.status())["revision"]
        r = await self.client.put(
            "/models/default",
            json={"revision": revision, "reference": "work/gemini-3.5-flash-lite"},
        )
        self.assertEqual(r.json()["default"], "work/gemini-3.5-flash-lite")
        revision = r.json()["revision"]
        r = await self.client.put(
            "/models/tasks", json={"revision": revision, "tasks": ["triage"], "reference": "flash"}
        )
        triage = next(t for t in r.json()["tasks"] if t["name"] == "triage")
        self.assertEqual((triage["reference"], triage["follows_default"]), ("flash", False))
        r = await self.client.put(
            "/models/tasks", json={"revision": r.json()["revision"], "tasks": ["triage"]}
        )
        self.assertTrue(
            next(t for t in r.json()["tasks"] if t["name"] == "triage")["follows_default"]
        )

    async def test_a_file_changed_since_it_was_read_is_not_overwritten(self) -> None:
        revision = (await self.status())["revision"]
        (self.dir / "models.toml").write_text(MODELS + "\n# edited by hand\n", encoding="utf-8")
        r = await self.client.put(
            "/models/default", json={"revision": revision, "reference": "flash"}
        )
        self.assertEqual(r.status_code, 409)
        self.assertIn("edited by hand", self.text())

    async def test_a_reference_to_nothing_is_refused_with_the_loaders_words(self) -> None:
        revision = (await self.status())["revision"]
        r = await self.client.put(
            "/models/default", json={"revision": revision, "reference": "nowhere/x"}
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("nowhere", r.text)

    async def test_a_provider_in_use_is_not_removed(self) -> None:
        revision = (await self.status())["revision"]
        r = await self.client.delete(f"/models/providers/work?revision={revision}")
        self.assertEqual(r.status_code, 422)
        self.assertIn("models.flash", r.text)


class AConversationsModels(_Api):
    async def test_are_its_own_and_checked(self) -> None:
        session = await self.sessions.create(workspace=self.dir, task="t")
        r = await self.client.get(f"/models/sessions/{session.id}")
        self.assertEqual(r.json()["effective"], "work/gemini-3.5-flash")
        r = await self.client.put(
            f"/models/sessions/{session.id}",
            json={"model": "work/gemini-3.5-flash-lite", "subagents": {"*": "flash"}},
        )
        self.assertEqual(r.json()["effective"], "work/gemini-3.5-flash-lite")
        self.assertEqual((await self.sessions.get(session.id)).models.subagents, (("*", "flash"),))
        r = await self.client.put(f"/models/sessions/{session.id}", json={"model": "nowhere"})
        self.assertEqual(r.status_code, 422)

    async def test_one_that_does_not_exist_is_404(self) -> None:
        self.assertEqual((await self.client.get("/models/sessions/nobody")).status_code, 404)


class ChangesAreBroadcast(_Api):
    async def test_a_write_puts_the_new_status_on_the_stream(self) -> None:
        sent: list = []

        async def broadcast(item) -> None:
            if getattr(item, "type", "") == "models":  # not the per-request http log lines
                sent.append(item)

        self.hub.broadcast = broadcast
        broadcast_model_changes(self.source, self.service, self.hub)
        revision = (await self.status())["revision"]
        await self.client.put(
            "/models/default",
            json={"revision": revision, "reference": "work/gemini-3.5-flash-lite"},
        )
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(sent[0].to_wire_dict()["models"]["default"], "work/gemini-3.5-flash-lite")


class Unconfigured(_Api):
    write_file = False

    async def test_says_so_and_a_setup_makes_it_configured(self) -> None:
        body = await self.status()
        self.assertEqual((body["configured"], body["exists"]), (False, False))
        r = await self.client.post(
            "/models/setup",
            json={
                "revision": body["revision"],
                "providers": [{"name": "local", "connection": {"type": "ollama"}}],
                "default": "local/qwen3:8b",
            },
        )
        self.assertTrue(r.json()["configured"], r.text)


if __name__ == "__main__":
    unittest.main()
