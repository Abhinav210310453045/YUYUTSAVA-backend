"""A daemon that cannot use any model waits to be set up instead of failing to boot."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.daemon.setup_mode import models_problem


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patch = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TheProblem(_Home):
    def test_nothing_configured_anywhere_is_one(self) -> None:
        self.assertIsNotNone(models_problem())

    def test_a_models_toml_that_does_not_load_is_one_in_the_loaders_words(self) -> None:
        (self.home / "models.toml").write_text("version = [", encoding="utf-8")
        self.assertIn("models.toml", models_problem())

    def test_a_usable_models_toml_is_none(self) -> None:
        (self.home / "models.toml").write_text(
            'version = 1\n[defaults]\nmodel = "local/qwen3:8b"\n[providers.local]\ntype = "ollama"\n',
            encoding="utf-8",
        )
        self.assertIsNone(models_problem())

    def test_a_working_environment_is_none(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"}):
            self.assertIsNone(models_problem())


if __name__ == "__main__":
    unittest.main()
