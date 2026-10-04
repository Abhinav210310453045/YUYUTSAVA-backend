"""``yuyutsava models``: what it shows, what it changes, and what it refuses.

Each case runs against a temporary ``YUYUTSAVA_HOME``; nothing calls a provider.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.cli.commands.models import run_models

MODELS = """\
# mine
version = 1

[defaults]
model = "fast"

[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:groq" }

[providers.local]
type = "ollama"

[models.fast]
provider = "groq"
id = "llama-3.1-8b-instant"
"""


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patch = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str = MODELS) -> None:
        (self.home / "models.toml").write_text(text, encoding="utf-8")

    def run_models(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_models(list(argv))
        return code, out.getvalue(), err.getvalue()


class WithoutAFile(_Home):
    def test_list_says_models_come_from_the_environment(self) -> None:
        code, out, _ = self.run_models("list")
        self.assertEqual(code, 0)
        self.assertIn("models come from the environment", out)
        self.assertIn("yuyutsava models setup", out)

    def test_use_points_at_setup(self) -> None:
        code, _, err = self.run_models("use", "groq/x")
        self.assertEqual(code, 1)
        self.assertIn("yuyutsava models setup", err)


class Showing(_Home):
    def test_list_shows_the_default_providers_and_models_without_colour_when_piped(self) -> None:
        self.write()
        code, out, _ = self.run_models("list")
        self.assertEqual(code, 0)
        self.assertIn("default      fast", out)
        self.assertIn("every task follows the default", out)
        self.assertIn("groq · llama-3.1-8b-instant", out)
        self.assertNotIn("\x1b[", out)

    def test_doctor_names_a_missing_secret(self) -> None:
        self.write()
        code, _, err = self.run_models("doctor")
        self.assertEqual(code, 1)
        self.assertIn("secret:groq", err)

    def test_doctor_is_happy_once_it_is_stored(self) -> None:
        self.write()
        (self.home / "secrets.toml").write_text('groq = "gsk-1"\n', encoding="utf-8")
        code, out, _ = self.run_models("doctor")
        self.assertEqual(code, 0)
        self.assertIn("chat", out)
        self.assertIn("groq/llama-3.1-8b-instant", out)

    def test_schema_is_json(self) -> None:
        code, out, _ = self.run_models("schema")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["title"], "YUYUTSAVA models.toml")


class Changing(_Home):
    def setUp(self) -> None:
        super().setUp()
        self.write()

    def text(self) -> str:
        return (self.home / "models.toml").read_text(encoding="utf-8")

    def test_use_sets_the_default_and_keeps_comments(self) -> None:
        code, out, _ = self.run_models("use", "local/qwen3:8b")
        self.assertEqual(code, 0)
        self.assertIn("the default model is now local/qwen3:8b", out)
        self.assertIn('model = "local/qwen3:8b"', self.text())
        self.assertIn("# mine", self.text())

    def test_use_for_a_task_then_follow_the_default_again(self) -> None:
        self.assertEqual(self.run_models("use", "local/qwen3:8b", "--task", "triage")[0], 0)
        self.assertIn('triage = "local/qwen3:8b"', self.text())
        self.assertEqual(self.run_models("follow", "triage")[0], 0)
        self.assertNotIn("triage", self.text())

    def test_a_reference_to_nothing_is_refused_and_nothing_written(self) -> None:
        before = self.text()
        code, _, err = self.run_models("use", "nowhere/x")
        self.assertEqual(code, 1)
        self.assertIn("nowhere", err)
        self.assertEqual(self.text(), before)

    def test_remove_refuses_a_provider_in_use_and_takes_one_that_is_not(self) -> None:
        code, _, err = self.run_models("remove", "groq")
        self.assertEqual(code, 1)
        self.assertIn("models.fast", err)
        self.assertEqual(self.run_models("remove", "local")[0], 0)
        self.assertNotIn("[providers.local]", self.text())

    def test_secrets_set_list_and_remove(self) -> None:
        with mock.patch.dict(os.environ, {"MY_KEY": "gsk-9"}):
            code, out, _ = self.run_models("secrets", "set", "groq", "--from-env", "MY_KEY")
        self.assertEqual(code, 0)
        self.assertIn("secret:groq", out)
        self.assertEqual(self.run_models("secrets", "list")[1].strip(), "groq")
        self.assertEqual(self.run_models("secrets", "rm", "groq")[0], 0)
        self.assertIn("no secrets stored", self.run_models("secrets", "list")[1])


class TalkingToProviders(_Home):
    def test_discover_reports_a_login_it_cannot_make_without_calling_out(self) -> None:
        self.write()
        code, _, err = self.run_models("discover", "groq")
        self.assertEqual(code, 1)
        self.assertIn("yuyutsava models secrets set groq", err)
        self.assertEqual(err.count(" — "), 1)  # one fix, not two

    def test_an_unknown_provider_is_named(self) -> None:
        self.write()
        code, _, err = self.run_models("verify", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no provider named nope", err)


if __name__ == "__main__":
    unittest.main()
