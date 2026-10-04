"""Which model each task uses: from ``models.toml`` when there is one, the environment until then.

``TaskModels`` is the one place that answers it. These pin how a task finds its
model in the file, that a file which never loaded is an error rather than a quiet
fallback to the environment, that an edit is in use from the next call (and
rebuilds the model), that each event loop gets its own instance, and how the
context window is found.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm.catalog import context_window
from yuyutsava.llm.config.loader import ModelsConfigError, load_text
from yuyutsava.llm.config.watch import ConfigSource
from yuyutsava.llm.selection import TaskModels, env_role
from yuyutsava.llm.target import target_for

MODELS = """\
version = 1
[defaults]
model = "fast"
[tasks]
triage = "local/llama3.2:3b"
subagent = "local/qwen3:8b"
"subagent.researcher" = "groq/llama-3.3-70b-versatile"
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:groq" }
[providers.local]
type = "ollama"
[models.fast]
provider = "groq"
id = "llama-3.1-8b-instant"
"""


class FromModelsToml(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.models_file = self.dir / "models.toml"
        self.models_file.write_text(MODELS, encoding="utf-8")
        (self.dir / "secrets.toml").write_text('groq = "gsk-1"\n', encoding="utf-8")
        self.source = ConfigSource(self.models_file, self.dir / "secrets.toml")
        self.source.reload()
        self.models = TaskModels(self.source)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_task_with_its_own_entry(self) -> None:
        target = self.models.target("triage")
        self.assertEqual((target.provider, target.model), ("local", "llama3.2:3b"))

    def test_a_task_without_one_uses_the_default(self) -> None:
        target = self.models.target("compaction")
        self.assertEqual((target.provider, target.model), ("groq", "llama-3.1-8b-instant"))
        self.assertEqual(target.secret("secret:groq"), "gsk-1")

    def test_a_named_subagent_then_all_subagents(self) -> None:
        self.assertEqual(self.models.target("subagent.researcher").model, "llama-3.3-70b-versatile")
        self.assertEqual(self.models.target("subagent.writer").model, "qwen3:8b")

    def test_an_unknown_task_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.models.target("compation")

    def test_an_edit_is_in_use_from_the_next_call_and_rebuilds_the_model(self) -> None:
        async def triage_model():
            return self.models.model("triage", temperature=0.0)

        async def twice_then_edit_then_once():
            first = await triage_model()
            again = await triage_model()
            self.models_file.write_text(
                MODELS.replace("local/llama3.2:3b", "local/gemma3:4b"), encoding="utf-8"
            )
            self.source.reload()
            return first, again, await triage_model()

        first, again, edited = asyncio.run(twice_then_edit_then_once())
        self.assertIs(first, again)
        self.assertIsNot(first, edited)
        self.assertEqual(edited.model_name, "gemma3:4b")
        self.assertEqual(self.models.origin, "models.toml")

    def test_a_rotated_key_rebuilds_the_same_model(self) -> None:
        async def default_model():
            return self.models.model("chat", temperature=0.1)

        async def before_and_after_rotation():
            before = await default_model()
            (self.dir / "secrets.toml").write_text('groq = "gsk-2"\n', encoding="utf-8")
            self.source.reload()
            return before, await default_model()

        before, after = asyncio.run(before_and_after_rotation())
        self.assertIsNot(before, after)
        self.assertEqual(after.openai_api_key.get_secret_value(), "gsk-2")

    def test_each_event_loop_gets_its_own_instance(self) -> None:
        async def one():
            return self.models.model("triage", temperature=0.0)

        self.assertIsNot(asyncio.run(one()), asyncio.run(one()))

    def test_a_bad_edit_keeps_the_working_config(self) -> None:
        self.models_file.write_text("version = [", encoding="utf-8")
        self.source.reload()
        self.assertEqual(self.models.target("triage").model, "llama3.2:3b")


class AFileThatNeverLoaded(unittest.TestCase):
    def test_is_an_error_not_a_fallback_to_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_file = Path(tmp) / "models.toml"
            models_file.write_text('version = 1\n[defaults]\nmodel = "nowhere"\n', encoding="utf-8")
            source = ConfigSource(models_file, Path(tmp) / "secrets.toml")
            source.reload()
            env = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "k"}
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(ModelsConfigError):
                TaskModels(source).target("chat")


class FromTheEnvironment(unittest.TestCase):
    def test_without_models_toml_each_task_reads_its_role(self) -> None:
        env = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "k", "TRIAGE_LLM_PROVIDER": "ollama"}
        with mock.patch.dict(os.environ, env, clear=True):
            models = TaskModels()
            self.assertEqual(models.target("chat").type, "groq")
            self.assertEqual(models.target("triage").type, "ollama")
            self.assertEqual(models.origin, "the environment")

    def test_the_roles_tasks_read(self) -> None:
        self.assertIsNone(env_role("chat"))
        self.assertEqual(env_role("compaction"), "compaction")
        self.assertEqual(env_role("subagent.researcher"), "subagent")


class WindowFromTheService(unittest.TestCase):
    """``TaskModels.window`` reads the built model's profile, on a loop or before one runs."""

    ENV = {
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_MODEL": "claude-haiku-4-5-20251001",
    }

    def test_without_a_running_loop(self) -> None:
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            models = TaskModels()
            self.assertEqual(models.window(models.target("chat")), 200_000)

    def test_on_a_running_loop(self) -> None:
        async def window():
            return models.window(models.target("chat"))

        with mock.patch.dict(os.environ, self.ENV, clear=True):
            models = TaskModels()
            self.assertEqual(asyncio.run(window()), 200_000)


class TheWindow(unittest.TestCase):
    CONFIG = load_text(
        "version = 1\n"
        '[defaults]\nmodel = "work/gemini-3.5-flash"\n'
        '[providers.work]\ntype = "vertex"\nproject = "p"\nlocation = "global"\n'
        'auth = { method = "adc" }\n'
        '[models.big]\nprovider = "work"\nid = "gemini-3.5-flash"\ncontext_window = 500000\n'
    )

    def _target(self, reference: str):
        return target_for(self.CONFIG, reference, secrets={}, unavailable={})

    def test_the_alias_wins(self) -> None:
        self.assertEqual(
            context_window(self._target("big"), {"max_input_tokens": 1_048_576}), 500_000
        )

    def test_then_the_models_profile(self) -> None:
        target = self._target("work/gemini-3.5-flash")
        self.assertEqual(context_window(target, {"max_input_tokens": 1_048_576}), 1_048_576)

    def test_then_the_family_claude_on_vertex_is_not_gemini(self) -> None:
        self.assertEqual(context_window(self._target("work/claude-sonnet-4-6")), 200_000)
        self.assertEqual(context_window(self._target("work/gemini-3.5-flash")), 1_000_000)
        self.assertEqual(context_window(self._target("work/openai/gpt-oss-120b-maas")), 128_000)


if __name__ == "__main__":
    unittest.main()
