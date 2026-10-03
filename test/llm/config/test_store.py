"""The store writes ``models.toml`` without losing comments, other writers' changes or validity."""

from __future__ import annotations

import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import tomlkit

from yuyutsava.llm.config.loader import ModelsConfigError
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.config.store import ABSENT, ConfigStore, StaleWrite

ORIGINAL = """\
# My models — written by hand.
version = 1

[defaults]
model = "fast"   # the everyday one

# Groq is quick and cheap.
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "env:GROQ_API_KEY" }

[models.fast]
provider = "groq"
id = "llama-3.3-70b-versatile"
"""


def set_default(model: str):
    def edit(document: tomlkit.TOMLDocument) -> None:
        document["defaults"]["model"] = model

    return edit


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.models = self.dir / "models.toml"
        self.store = ConfigStore(self.models, self.dir / "secrets.toml")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class AnEditKeepsWhatItDidNotTouch(StoreTest):
    def test_comments_and_layout_survive_byte_for_byte(self) -> None:
        self.models.write_text(ORIGINAL, encoding="utf-8")
        revision = self.store.apply(
            set_default("groq/llama-3.1-8b-instant"), expected=self.store.read().digest
        )
        self.assertEqual(
            revision.text,
            ORIGINAL.replace('model = "fast"   #', 'model = "groq/llama-3.1-8b-instant"   #'),
        )
        self.assertEqual(self.models.read_text(encoding="utf-8"), revision.text)

    def test_a_new_file_is_created_from_nothing(self) -> None:
        self.assertEqual(self.store.read().digest, ABSENT)

        def first_setup(document: tomlkit.TOMLDocument) -> None:
            document["version"] = 1
            document["defaults"] = {"model": "groq/llama-3.3-70b-versatile"}
            groq = tomlkit.table()
            groq["type"] = "groq"
            groq["auth"] = {"method": "api_key", "key": "secret:groq"}
            document["providers"] = {"groq": groq}

        revision = self.store.apply(first_setup, expected=ABSENT)
        self.assertEqual(self.models.read_text(encoding="utf-8"), revision.text)
        self.assertIn('[providers.groq]\ntype = "groq"', revision.text)


class AWriteThatWouldBreakTheFileIsRefused(StoreTest):
    def test_nothing_is_written(self) -> None:
        self.models.write_text(ORIGINAL, encoding="utf-8")
        with self.assertRaises(ModelsConfigError) as caught:
            self.store.apply(set_default("nope"), expected=self.store.read().digest)
        self.assertEqual(caught.exception.issues[0].path, "defaults.model")
        self.assertEqual(self.models.read_text(encoding="utf-8"), ORIGINAL)


class AChangeMadeElsewhereIsNeverOverwritten(StoreTest):
    def test_a_write_based_on_an_old_revision_is_refused(self) -> None:
        self.models.write_text(ORIGINAL, encoding="utf-8")
        seen = self.store.read()
        self.models.write_text(ORIGINAL.replace("Groq is quick", "Groq is fast"), encoding="utf-8")
        with self.assertRaises(StaleWrite):
            self.store.apply(set_default("groq/x"), expected=seen.digest)
        self.assertIn("Groq is fast", self.models.read_text(encoding="utf-8"))

    def test_of_two_writers_from_the_same_revision_one_wins(self) -> None:
        self.models.write_text(ORIGINAL, encoding="utf-8")
        seen = self.store.read().digest
        outcomes: list[str] = []
        start = threading.Barrier(2)

        def writer(model: str) -> None:
            start.wait()
            try:
                ConfigStore(self.models, self.dir / "secrets.toml").apply(
                    set_default(model), expected=seen
                )
                outcomes.append("wrote")
            except StaleWrite:
                outcomes.append("stale")

        threads = [threading.Thread(target=writer, args=(f"groq/model-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["stale", "wrote"])


class Secrets(StoreTest):
    def test_a_stored_secret_is_found_and_the_file_is_owner_only(self) -> None:
        ref = self.store.store_secret("groq", "gsk-1\n")
        self.assertEqual(str(ref), "secret:groq")
        path = self.dir / "secrets.toml"
        self.assertEqual(read_secrets_file(path), {"groq": "gsk-1"})
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_other_secrets_and_comments_are_kept(self) -> None:
        self.store.store_secret("groq", "gsk-1")
        self.store.store_secret("openrouter", "sk-or-1")
        self.store.store_secret("groq", "gsk-2")
        text = (self.dir / "secrets.toml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Secrets that models.toml refers to"))
        self.assertEqual(
            read_secrets_file(self.dir / "secrets.toml"), {"groq": "gsk-2", "openrouter": "sk-or-1"}
        )

    def test_forgetting_a_secret(self) -> None:
        self.store.store_secret("groq", "gsk-1")
        self.assertTrue(self.store.forget_secret("groq"))
        self.assertFalse(self.store.forget_secret("groq"))
        self.assertEqual(read_secrets_file(self.dir / "secrets.toml"), {})

    def test_a_bad_name_or_an_empty_value_is_refused_without_echoing_the_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.store_secret("my key", "sk-very-secret")
        self.assertNotIn("sk-very-secret", str(caught.exception))
        with self.assertRaises(ValueError):
            self.store.store_secret("groq", "   ")


class TheCurrentRevisionIsAccepted(StoreTest):
    """The other side of the revision check: a write based on what is there now lands."""

    def test_after_an_outside_change_the_new_revision_writes(self) -> None:
        self.models.write_text(ORIGINAL, encoding="utf-8")
        self.models.write_text(ORIGINAL + "\n", encoding="utf-8")
        self.store.apply(set_default("groq/x"), expected=self.store.read().digest)
        self.assertIn('model = "groq/x"', self.models.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
