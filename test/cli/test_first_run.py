"""The first chat on a machine without models.toml offers the setup wizard — once, and only at a terminal."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.cli.first_run import (
    offer_setup,
    setup_declined_path,
    setup_offer,
    warn_leftovers,
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TheOffer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patch = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.asked: list[str] = []
        self.ran: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def offer(self, answer: str = "now", *, tty: bool = True) -> bool:
        stream = _Tty() if tty else io.StringIO()

        def ask() -> str:
            self.asked.append("asked")
            return answer

        return offer_setup(
            ask=ask, run_setup=lambda: self.ran.append("setup") or 0, stdin=stream, stdout=stream
        )

    def test_at_a_terminal_without_models_toml_it_is_offered_and_run(self) -> None:
        self.offer("now")
        self.assertEqual((self.asked, self.ran), (["asked"], ["setup"]))

    def test_never_from_a_script(self) -> None:
        self.offer(tty=False)
        self.assertEqual(self.asked, [])

    def test_never_once_models_toml_exists(self) -> None:
        (self.home / "models.toml").write_text("version = 1\n")
        self.assertFalse(self.offer())  # not offered: the chat's start may warn instead
        self.assertEqual(self.asked, [])

    def test_says_when_it_was_offered_so_the_start_does_not_repeat_it(self) -> None:
        self.assertTrue(self.offer("later"))

    def test_keeping_the_environment_is_remembered(self) -> None:
        self.offer("never")
        self.assertTrue(setup_declined_path().exists())
        self.offer("now")
        self.assertEqual((self.asked, self.ran), (["asked"], []))

    def test_not_now_asks_again_next_time(self) -> None:
        self.offer("later")
        self.offer("later")
        self.assertEqual(self.asked, ["asked", "asked"])
        self.assertFalse(setup_declined_path().exists())

    def test_the_environment_can_turn_it_off(self) -> None:
        with mock.patch.dict(os.environ, {"YUYUTSAVA_SKIP_SETUP": "1"}):
            self.offer()
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()


class TheLeftoverWarning(unittest.TestCase):
    """Once models.toml exists, a chat's start names the model variables still set."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "models.toml").write_text(
            'version = 1\n[defaults]\nmodel = "local/qwen3:8b"\n[providers.local]\ntype = "ollama"\n'
        )
        for patch in (
            mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)}, clear=True),
            mock.patch("yuyutsava.llm.config.leftovers.find_dotenv", return_value=""),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def warning(self) -> str:
        out = io.StringIO()
        warn_leftovers(stderr=out)
        return out.getvalue()

    def test_names_them_and_the_file_they_are_in(self) -> None:
        (self.home / ".env").write_text("LLM_PROVIDER=vertex\nTAVILY_API_KEY=t\n")
        said = self.warning()
        self.assertIn("LLM_PROVIDER", said)
        self.assertIn(str(self.home / ".env"), said)
        self.assertNotIn("TAVILY", said)
        self.assertIn("yuyutsava models clean-env", said)
        self.assertNotIn("\x1b", said)  # not a terminal: no colour codes

    def test_says_nothing_when_none_is_set(self) -> None:
        self.assertEqual(self.warning(), "")


class WhatTheOfferSays(unittest.TestCase):
    """A newcomer is told what is in use now and where it is written, in plain words."""

    def test_names_the_model_in_use_and_the_file_it_is_set_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, ".env")
            env.write_text("LLM_PROVIDER=groq\n")
            setting = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"}
            with mock.patch.dict(os.environ, setting, clear=True):
                offer = setup_offer(os.environ, [env])
        said = " ".join(offer.lines)
        self.assertIn("Groq · llama-3.3-70b-versatile", said)
        self.assertIn(str(env), said)
        labels = [label for _, label, _ in offer.choices]
        # The file by name, not "that .env": a person cannot see what is being
        # pointed at, and there may be two of them.
        self.assertTrue(labels[-1].startswith("No, keep using "))
        self.assertIn(env.name, labels[-1])
        self.assertNotIn("that .env", labels[-1])
        self.assertNotIn("environment", " ".join(labels))

    def test_offers_no_keep_choice_when_nothing_is_set(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            offer = setup_offer(os.environ, [])
        self.assertIn("doesn't know which AI model to use yet", offer.lines[0])
        self.assertEqual([value for value, _, _ in offer.choices], ["now", "later"])
