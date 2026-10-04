"""The edits every surface makes to models.toml: they keep what a person wrote, and refuse
to leave a reference dangling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.llm.config.edits import (
    ConfigEditError,
    add_model,
    add_provider,
    combine,
    follow_default,
    keep_keys_in_secrets,
    remove_model,
    remove_provider,
    use,
)
from yuyutsava.llm.config.loader import ModelsConfigError, load_text
from yuyutsava.llm.config.store import ABSENT, ConfigStore

GROQ = {"type": "groq", "auth": {"method": "api_key", "key": "secret:groq"}}
LOCAL = {"type": "ollama"}

HAND_WRITTEN = """\
# my models — written by hand
version = 1

[defaults]
model = "local/qwen3:8b"   # the small one

[providers.local]
type = "ollama"
"""


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = ConfigStore(self.dir / "models.toml", self.dir / "secrets.toml")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def apply(self, *edits):
        return self.store.apply(combine(*edits), expected=self.store.read().digest)

    def config(self):
        return load_text(self.store.read().text)


class AFirstSetup(_Store):
    def test_writes_a_valid_file_with_its_header_and_the_default_first(self) -> None:
        self.assertEqual(self.store.read().digest, ABSENT)
        self.apply(add_provider("groq", GROQ), use("groq/llama-3.3-70b-versatile"))
        text = self.store.read().text
        self.assertTrue(text.startswith("# YUYUTSAVA models"))
        self.assertLess(text.index("[defaults]"), text.index("[providers.groq]"))
        self.assertIn('auth = {method = "api_key", key = "secret:groq"}', text)
        self.assertEqual(self.config().defaults.model, "groq/llama-3.3-70b-versatile")

    def test_without_a_default_nothing_is_written(self) -> None:
        with self.assertRaises(ModelsConfigError):
            self.apply(add_provider("groq", GROQ))
        self.assertEqual(self.store.read().digest, ABSENT)


class AHandWrittenFile(_Store):
    def setUp(self) -> None:
        super().setUp()
        (self.dir / "models.toml").write_text(HAND_WRITTEN, encoding="utf-8")

    def test_keeps_its_comments_through_every_edit(self) -> None:
        self.apply(
            add_provider("groq", GROQ),
            add_model(
                "fast", provider="groq", model_id="llama-3.1-8b-instant", context_window=131072
            ),
            use("fast", tasks=["triage", "subagent.researcher"]),
        )
        text = self.store.read().text
        self.assertIn("# my models — written by hand", text)
        self.assertIn("# the small one", text)
        config = self.config()
        self.assertEqual(config.tasks, {"triage": "fast", "subagent.researcher": "fast"})
        self.assertEqual(config.models["fast"].context_window, 131072)

    def test_a_task_can_follow_the_default_again(self) -> None:
        self.apply(use("local/gemma3:4b", tasks=["triage"]))
        self.apply(follow_default(["triage"]))
        self.assertEqual(self.config().tasks, {})

    def test_an_unknown_task_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaises(ConfigEditError):
            use("local/x", tasks=["triag"])


class References(_Store):
    def setUp(self) -> None:
        super().setUp()
        (self.dir / "models.toml").write_text(HAND_WRITTEN, encoding="utf-8")
        self.apply(
            add_provider("groq", GROQ),
            add_model("fast", provider="groq", model_id="llama-3.1-8b-instant"),
            use("fast", tasks=["triage"]),
        )

    def test_a_provider_in_use_is_not_removed_and_says_where(self) -> None:
        with self.assertRaises(ConfigEditError) as caught:
            self.apply(remove_provider("groq"))
        self.assertIn("models.fast", caught.exception.problem)
        self.assertIn("[providers.groq]", self.store.read().text)

    def test_a_model_in_use_is_not_removed(self) -> None:
        with self.assertRaises(ConfigEditError) as caught:
            self.apply(remove_model("fast"))
        self.assertIn("tasks.triage", caught.exception.problem)

    def test_once_freed_both_go(self) -> None:
        self.apply(follow_default(["triage"]), remove_model("fast"), remove_provider("groq"))
        self.assertEqual(set(self.config().providers), {"local"})

    def test_a_second_provider_of_one_name_is_refused(self) -> None:
        with self.assertRaises(ConfigEditError):
            self.apply(add_provider("groq", LOCAL))

    def test_a_model_on_a_missing_provider_is_refused(self) -> None:
        with self.assertRaises(ConfigEditError):
            self.apply(add_model("x", provider="nowhere", model_id="m"))


class KeysFromTheEnvironment(_Store):
    def test_are_copied_into_secrets_and_referred_to_there(self) -> None:
        connection = {"type": "groq", "auth": {"method": "api_key", "key": "env:GROQ_API_KEY"}}
        kept = keep_keys_in_secrets(
            connection,
            provider="groq",
            store_secret=self.store.store_secret,
            environ={"GROQ_API_KEY": "gsk-1"},
        )
        self.assertEqual(kept["auth"]["key"], "secret:groq")
        self.assertIn('groq = "gsk-1"', (self.dir / "secrets.toml").read_text())

    def test_a_variable_that_is_not_set_stays_a_reference(self) -> None:
        connection = {"type": "groq", "auth": {"method": "api_key", "key": "env:GROQ_API_KEY"}}
        kept = keep_keys_in_secrets(
            connection, provider="groq", store_secret=self.store.store_secret, environ={}
        )
        self.assertEqual(kept["auth"]["key"], "env:GROQ_API_KEY")


if __name__ == "__main__":
    unittest.main()
