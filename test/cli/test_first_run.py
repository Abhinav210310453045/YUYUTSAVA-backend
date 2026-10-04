"""The first chat on a machine without models.toml offers the setup wizard — once, and only at a terminal."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.cli.first_run import offer_setup, setup_declined_path


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

    def offer(self, answer: str = "now", *, tty: bool = True) -> None:
        stream = _Tty() if tty else io.StringIO()

        def ask() -> str:
            self.asked.append("asked")
            return answer

        offer_setup(
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
        self.offer()
        self.assertEqual(self.asked, [])

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
