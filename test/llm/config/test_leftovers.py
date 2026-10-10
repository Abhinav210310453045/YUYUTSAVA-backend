"""Once models.toml decides the models, the environment's model variables are not read —
and each surface can name the ones still set and remove them from the .env they are in,
without touching anything else written there (design §7, M7)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm import settings
from yuyutsava.llm.config.leftovers import env_files, find_leftovers, remove_leftovers
from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.watch import ConfigSource
from yuyutsava.llm.selection import TaskModels

MODELS = """\
version = 1
[defaults]
model = "groq/llama-3.3-70b-versatile"
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:groq" }
[providers.claude]
type = "anthropic"
auth = { method = "api_key", key = "env:ANTHROPIC_API_KEY" }
"""

SECRETS = {"groq": "gsk-kept", "openrouter": "sk-or-kept"}


def _names(found) -> list[str]:
    return [leftover.name for leftover in found]


class TheEnvironmentRoute(unittest.TestCase):
    """The variables the env route reads are all listed, so none goes unnamed."""

    def test_every_variable_a_reader_reads_is_listed(self) -> None:
        listed = settings.SETTING_VARIABLES | settings.KEY_VARIABLES | settings.SHARED_VARIABLES
        for provider in settings.PROVIDER_TYPES:
            read: set[str] = set()

            def env(name, role=None, default="", _read=read, _provider=provider):
                _read.add(name)
                return _provider if name == "LLM_PROVIDER" else "1"

            def env_int(name, role=None, default=0, _read=read):
                _read.add(name)
                return 1

            with (
                self.subTest(provider=provider),
                mock.patch.object(settings, "env", env),
                mock.patch.object(settings, "env_int", env_int),
            ):
                settings.env_setup()
                self.assertLessEqual(read, listed, f"{provider} reads unlisted variables")


class Leftovers(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_text(MODELS)

    def find(self, environ, files=()):
        return find_leftovers(self.config, secrets=SECRETS, environ=environ, files=files)

    def test_names_the_settings_with_or_without_a_tasks_prefix(self) -> None:
        environ = {
            "LLM_PROVIDER": "vertex",
            "VERTEX_MODEL": "gemini-3.5-flash",
            "TRIAGE_LLM_PROVIDER": "ollama",
            "TIER_LIGHT_OLLAMA_MODEL": "llama3.2:3b",
            "EMBED_LLM_PROVIDER": "ollama",  # the embedder's, not a model task's
            "GOOGLE_CLOUD_PROJECT": "p",  # gcloud reads it too
            "PATH": "/bin",
        }
        found = self.find(environ)
        self.assertEqual(
            _names(found),
            ["LLM_PROVIDER", "TIER_LIGHT_OLLAMA_MODEL", "TRIAGE_LLM_PROVIDER", "VERTEX_MODEL"],
        )
        self.assertEqual({leftover.kind for leftover in found}, {"setting"})

    def test_names_a_key_only_when_secrets_toml_keeps_it_and_nothing_reads_it(self) -> None:
        environ = {
            "OPENROUTER_API_KEY": "sk-or-kept",  # copied into secrets.toml: left over
            "GROQ_API_KEY": "gsk-kept",  # voice speech-to-text still reads it
            "ANTHROPIC_API_KEY": "sk-ant",  # models.toml reads it: env:ANTHROPIC_API_KEY
            "OPENAI_API_KEY": "sk-other",  # not in secrets.toml: someone else's
        }
        found = self.find(environ)
        self.assertEqual(_names(found), ["OPENROUTER_API_KEY"])
        self.assertEqual(found[0].kind, "key")
        self.assertIn("secrets.toml", found[0].why)

    def test_says_which_files_set_each_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, home = Path(tmp, "project.env"), Path(tmp, "home.env")
            project.write_text("LLM_PROVIDER=vertex\n", encoding="utf-8")
            home.write_text("LLM_PROVIDER=vertex\nVERTEX_MODEL=x\n", encoding="utf-8")
            found = self.find({"GROQ_MODEL": "y"}, files=[project, home])
        files = {leftover.name: leftover.files for leftover in found}
        self.assertEqual(files["LLM_PROVIDER"], (project, home))
        self.assertEqual(files["VERTEX_MODEL"], (home,))
        self.assertEqual(files["GROQ_MODEL"], ())  # the shell's: only its owner can unset it


class TheEnvFiles(unittest.TestCase):
    def test_are_the_projects_then_the_homes_when_they_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, home = Path(tmp, "project.env"), Path(tmp, "home")
            project.write_text("A=1\n")
            home.mkdir()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("yuyutsava.llm.config.leftovers.find_dotenv", return_value=str(project)),
            ):
                self.assertEqual(env_files(home), (project,))
                (home / ".env").write_text("B=1\n")
                self.assertEqual(env_files(home), (project, home / ".env"))
                os.environ["PYTHON_DOTENV_DISABLED"] = "1"  # python-dotenv loads none
                self.assertEqual(env_files(home), ())


class Removing(unittest.TestCase):
    ENV = (
        "# my settings\n"
        "LLM_PROVIDER=vertex\n"
        "export VERTEX_MODEL='gemini-3.5-flash'   # the one I use\n"
        "\n"
        "TAVILY_API_KEY=tvly-1\n"
        "OPENROUTER_API_KEY=sk-or-kept\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name, ".env")
        self.path.write_text(self.ENV, encoding="utf-8")
        found = find_leftovers(
            load_text(MODELS), secrets=SECRETS, environ={"GROQ_MODEL": "y"}, files=[self.path]
        )
        self.environ = {"LLM_PROVIDER": "vertex", "GROQ_MODEL": "y", "TAVILY_API_KEY": "tvly-1"}
        self.changed = remove_leftovers(found, environ=self.environ)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_removes_only_their_lines(self) -> None:
        self.assertEqual(self.changed, [self.path])
        self.assertEqual(
            self.path.read_text(encoding="utf-8"), "# my settings\n\nTAVILY_API_KEY=tvly-1\n"
        )

    def test_takes_them_out_of_the_process_too_but_not_what_the_shell_set(self) -> None:
        self.assertEqual(self.environ, {"GROQ_MODEL": "y", "TAVILY_API_KEY": "tvly-1"})

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_leaves_the_file_readable_by_its_owner_alone(self) -> None:
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_keeps_a_symlinked_env_a_symlink(self) -> None:
        real = Path(self._tmp.name, "real.env")
        real.write_text("LLM_PROVIDER=groq\nKEEP=1\n", encoding="utf-8")
        link = Path(self._tmp.name, "link.env")
        link.symlink_to(real)
        found = find_leftovers(load_text(MODELS), secrets={}, environ={}, files=[link])
        remove_leftovers(found, environ={})
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), "KEEP=1\n")


class AfterSetup(unittest.TestCase):
    """The M7 exit checks: the environment's model variables are not read, and a key
    kept in secrets.toml never reaches the process environment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        (home / "models.toml").write_text(MODELS, encoding="utf-8")
        (home / "secrets.toml").write_text('groq = "gsk-kept"\n', encoding="utf-8")
        self.source = ConfigSource(home / "models.toml", home / "secrets.toml")
        self.source.reload()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_environment_does_not_choose_the_model(self) -> None:
        env = {"LLM_PROVIDER": "vertex", "VERTEX_PROJECT": "p", "VERTEX_MODEL": "gemini-x"}
        with mock.patch.dict(os.environ, env, clear=True):
            target = TaskModels(self.source).target("chat")
        self.assertEqual(target.reference, "groq/llama-3.3-70b-versatile")

    def test_a_key_from_secrets_toml_stays_out_of_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            TaskModels(self.source).build("chat", temperature=0.0)
            self.assertNotIn("gsk-kept", os.environ.values())


if __name__ == "__main__":
    unittest.main()
