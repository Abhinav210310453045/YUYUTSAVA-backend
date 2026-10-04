"""``/model`` in a chat: listing, switching this conversation, its subagents, and the default.

Against a real session store and models.toml in a temporary folder; nothing is built.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.cli.model_command import ModelCommand
from yuyutsava.cli.render.ansi_wrap import strip_ansi
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.config.watch import ConfigSource
from yuyutsava.llm.selection import TaskModels
from yuyutsava.storage.models import ALL_SUBAGENTS, ModelChoice
from yuyutsava.storage.sessions import SqliteSessionStore

MODELS = """\
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
[models.lite]
provider = "work"
id = "gemini-3.5-flash-lite"
"""


class _Chat(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "models.toml").write_text(MODELS, encoding="utf-8")
        source = ConfigSource(self.dir / "models.toml", self.dir / "secrets.toml")
        source.reload()
        self.sessions = SqliteSessionStore(self.dir / "sessions.db")
        self.session = await self.sessions.create(workspace=self.dir, task="t")
        self.command = ModelCommand(
            models=TaskModels(source),
            sessions=self.sessions,
            store=ConfigStore(self.dir / "models.toml", self.dir / "secrets.toml"),
            subagents=("general-purpose",),
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def model(self, *args: str) -> str:
        lines = await self.command.run(self.session.id, list(args))
        return strip_ansi("\n".join(lines))

    async def choice(self) -> ModelChoice:
        return (await self.sessions.get(self.session.id)).models


class Listing(_Chat):
    async def test_numbers_every_model_and_marks_this_chats(self) -> None:
        shown = await self.model()
        self.assertIn("● 1. flash  work · gemini-3.5-flash · default", shown)
        self.assertIn("○ 2. lite  work · gemini-3.5-flash-lite", shown)
        self.assertIn("this chat follows models.toml", shown)


class Switching(_Chat):
    async def test_by_number_is_this_chats_from_the_next_message_and_nudges_the_subagents(
        self,
    ) -> None:
        shown = await self.model("2")
        self.assertIn("this chat runs on lite from your next message", shown)
        self.assertIn("subagents (general-purpose) still use flash", shown)
        self.assertIn("/model subagents all", shown)
        self.assertEqual((await self.choice()).model, "lite")
        self.assertIn("● 2. lite", await self.model())

    async def test_by_name_including_provider_and_id(self) -> None:
        await self.model("work/gemini-3.5-flash-lite")
        self.assertEqual((await self.choice()).model, "work/gemini-3.5-flash-lite")

    async def test_a_model_that_is_not_there_changes_nothing(self) -> None:
        shown = await self.model("nowhere")
        self.assertIn("models.toml has no model 'nowhere'", shown)
        self.assertEqual(await self.choice(), ModelChoice())

    async def test_reset_follows_models_toml_again(self) -> None:
        await self.model("lite")
        await self.model("reset")
        self.assertEqual(await self.choice(), ModelChoice())


class Subagents(_Chat):
    async def test_all_follow_the_chats_model_and_the_nudge_stops(self) -> None:
        await self.model("lite")
        shown = await self.model("subagents", "all")
        self.assertIn("all subagents in this chat run on lite", shown)
        self.assertEqual((await self.choice()).subagents, ((ALL_SUBAGENTS, "lite"),))
        self.assertNotIn("still use", await self.model("lite"))

    async def test_one_by_name_with_its_own_model(self) -> None:
        await self.model("subagents", "general-purpose", "1")
        self.assertEqual((await self.choice()).subagents, (("general-purpose", "flash"),))

    async def test_one_this_chat_does_not_have_is_named(self) -> None:
        shown = await self.model("subagents", "researcher")
        self.assertIn("no subagent 'researcher' in this chat (general-purpose)", shown)


class TheDefault(_Chat):
    async def test_is_written_to_models_toml_and_used_at_once(self) -> None:
        shown = await self.model("default", "lite")
        self.assertIn("every chat's default is now lite", shown)
        self.assertIn('model = "lite"', (self.dir / "models.toml").read_text())
        self.assertEqual(self.command.models.target("chat").model, "gemini-3.5-flash-lite")


class WithoutModelsToml(unittest.IsolatedAsyncioTestCase):
    async def test_says_how_to_get_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SqliteSessionStore(Path(tmp) / "s.db")
            session = await sessions.create(workspace=Path(tmp), task="t")
            command = ModelCommand(
                models=TaskModels(),
                sessions=sessions,
                store=ConfigStore(Path(tmp) / "models.toml", Path(tmp) / "secrets.toml"),
            )
            shown = strip_ansi("\n".join(await command.run(session.id, [])))
        self.assertIn("yuyutsava models setup", shown)


if __name__ == "__main__":
    unittest.main()
