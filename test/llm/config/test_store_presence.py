"""With ``[security] require_user_presence``, the store has the owner confirm a key change —
and turning that check off — before it writes; without it, nothing is asked."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.llm.config.edits import ConfigEditError, require_presence, use
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.platform.user_presence import PresenceNotConfirmed

MODELS = """\
version = 1
[defaults]
model = "local/qwen3:8b"
[providers.local]
type = "ollama"
"""
GUARDED = MODELS + "[security]\nrequire_user_presence = true\n"


class _Gate:
    """A presence gate that answers as told and records every reason it was asked for."""

    def __init__(self, *, confirms: bool = True, mechanism: str | None = "Touch ID") -> None:
        self.confirms = confirms
        self.mechanism = mechanism
        self.asked: list[str] = []

    def confirm(self, reason: str):
        self.asked.append(reason)
        if not self.confirms:
            raise PresenceNotConfirmed("Touch ID was cancelled or refused")


class _Store(unittest.TestCase):
    text = MODELS

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "models.toml").write_text(self.text, encoding="utf-8")
        self.gate = _Gate()
        self.store = ConfigStore(
            self.dir / "models.toml", self.dir / "secrets.toml", presence=lambda: self.gate
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def apply(self, edit):
        return self.store.apply(edit, expected=self.store.read().digest)

    def secrets(self) -> dict[str, str]:
        return read_secrets_file(self.dir / "secrets.toml")


class WithoutTheCheck(_Store):
    def test_keys_change_without_asking(self) -> None:
        self.store.store_secret("groq", "gsk-1")
        self.store.forget_secret("groq")
        self.assertEqual(self.gate.asked, [])

    def test_turning_it_on_asks_nothing_and_needs_a_way_to_ask(self) -> None:
        self.apply(require_presence(True))
        self.assertTrue(self.store.presence_required())
        self.assertEqual(self.gate.asked, [])

    def test_it_is_not_turned_on_where_nothing_can_ask(self) -> None:
        self.gate.mechanism = None
        with self.assertRaises(ConfigEditError) as caught:
            self.apply(require_presence(True))
        self.assertIn("no way to confirm", caught.exception.problem)
        self.assertFalse(self.store.presence_required())


class WithTheCheck(_Store):
    text = GUARDED

    def test_a_key_is_stored_once_confirmed_saying_which(self) -> None:
        self.store.store_secret("groq", "gsk-1")
        self.assertEqual(self.gate.asked, ["change the key secret:groq"])
        self.assertEqual(self.secrets(), {"groq": "gsk-1"})

    def test_a_refusal_writes_nothing(self) -> None:
        self.gate.confirms = False
        with self.assertRaises(PresenceNotConfirmed):
            self.store.store_secret("groq", "gsk-1")
        self.assertEqual(self.secrets(), {})
        with self.assertRaises(PresenceNotConfirmed):
            self.store.forget_secret("groq")

    def test_turning_it_off_needs_it_passed(self) -> None:
        self.gate.confirms = False
        with self.assertRaises(PresenceNotConfirmed):
            self.apply(require_presence(False))
        self.assertTrue(self.store.presence_required())
        self.gate.confirms = True
        self.apply(require_presence(False))
        self.assertFalse(self.store.presence_required())
        self.assertNotIn("[security]", self.store.read().text)

    def test_other_changes_ask_nothing(self) -> None:
        self.apply(use("local/llama3.2:3b"))
        self.assertEqual(self.gate.asked, [])

    def test_a_mistake_elsewhere_in_the_file_does_not_switch_it_off(self) -> None:
        broken = GUARDED.replace('type = "ollama"', 'type = "nonsense"')
        (self.dir / "models.toml").write_text(broken, encoding="utf-8")
        self.store.store_secret("groq", "gsk-1")
        self.assertEqual(len(self.gate.asked), 1)


if __name__ == "__main__":
    unittest.main()
