"""What the setup wizard offers to import: the logins and keys this machine already has.

Each case builds a machine in a temporary home — gcloud's folder is pointed there
too (``CLOUDSDK_CONFIG``) — so the developer's own logins never leak in.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm.config.detect import detect


def _nothing_answers(url: str):
    raise OSError("connection refused")


class _Machine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "gcloud").mkdir()
        self.environ = {"CLOUDSDK_CONFIG": str(self.home / "gcloud"), "PATH": ""}
        self.dotenv_files: list[Path] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def found(self, probe=_nothing_answers):
        with mock.patch.dict(os.environ, self.environ, clear=True):
            return detect(home=self.home, probe=probe, dotenv_files=self.dotenv_files)


class AnEmptyMachine(_Machine):
    def test_has_nothing_to_offer(self) -> None:
        self.assertEqual(self.found(), [])


class TheCurrentSetup(_Machine):
    def test_comes_first_with_its_model_and_keys_as_references(self) -> None:
        self.environ.update(
            {
                "LLM_PROVIDER": "groq",
                "GROQ_API_KEY": "gsk-1",
                "GROQ_MODEL": "llama-3.3-70b-versatile",
            }
        )
        first = self.found()[0]
        self.assertEqual((first.type, first.model), ("groq", "llama-3.3-70b-versatile"))
        self.assertIn("LLM_PROVIDER=groq", first.origin)
        self.assertEqual(first.connection["auth"]["key"], "env:GROQ_API_KEY")
        self.assertNotIn("gsk-1", json.dumps(dict(first.connection)))

    def test_says_which_file_it_is_set_in_and_its_model(self) -> None:
        env = self.home / ".env"
        env.write_text("LLM_PROVIDER=groq\n")
        self.dotenv_files = [env]
        self.environ.update({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"})
        origin = self.found()[0].origin
        self.assertIn("what YUYUTSAVA uses today: llama-3.3-70b-versatile", origin)
        self.assertIn(f"set in {env}", origin)

    def test_set_outside_any_file_says_so(self) -> None:
        self.environ.update({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"})
        self.assertIn("your terminal's environment", self.found()[0].origin)

    def test_an_incomplete_one_is_skipped(self) -> None:
        self.environ["LLM_PROVIDER"] = "vertex"  # no project
        self.assertEqual([f for f in self.found() if "LLM_PROVIDER" in f.origin], [])

    def test_the_same_connection_found_twice_is_offered_once(self) -> None:
        self.environ.update({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"})
        self.assertEqual(len([f for f in self.found() if f.type == "groq"]), 1)


class AGoogleLogin(_Machine):
    def _credentials(self, kind: str) -> str:
        path = self.home / "creds.json"
        path.write_text(json.dumps({"type": kind}), encoding="utf-8")
        return str(path)

    def test_gcloud_application_default_credentials(self) -> None:
        (self.home / "gcloud" / "application_default_credentials.json").write_text("{}")
        self.environ["VERTEX_PROJECT"] = "p-1"
        vertex = self.found()[0]
        self.assertEqual(vertex.connection["auth"], {"method": "adc"})
        self.assertEqual(vertex.connection["project"], "p-1")
        self.assertIn("Google account", vertex.origin)
        self.assertIn("project p-1", vertex.origin)

    def test_a_service_account_key(self) -> None:
        key = self._credentials("service_account")
        self.environ.update({"GOOGLE_APPLICATION_CREDENTIALS": key, "VERTEX_PROJECT": "p-1"})
        auth = self.found()[0].connection["auth"]
        self.assertEqual(auth, {"method": "service_account", "key_file": key})

    def test_a_user_login_saved_to_a_file_is_not_a_service_account(self) -> None:
        key = self._credentials("authorized_user")
        self.environ.update({"GOOGLE_APPLICATION_CREDENTIALS": key, "VERTEX_PROJECT": "p-1"})
        self.assertEqual(self.found()[0].connection["auth"], {"method": "adc"})

    def test_without_a_project_there_is_nothing_to_write(self) -> None:
        (self.home / "gcloud" / "application_default_credentials.json").write_text("{}")
        self.assertEqual([f for f in self.found() if f.type == "vertex"], [])


class AwsProfiles(_Machine):
    def test_each_profile_with_its_region(self) -> None:
        aws = self.home / ".aws"
        aws.mkdir()
        (aws / "config").write_text(
            "[default]\nregion = us-west-2\n[profile work]\nregion = eu-west-1\n"
        )
        (aws / "credentials").write_text("[default]\naws_access_key_id = x\n")
        found = {f.name: f.connection for f in self.found()}
        self.assertEqual(found["bedrock"]["region"], "us-west-2")
        self.assertEqual(
            found["bedrock-work"]["auth"], {"method": "aws_profile", "profile": "work"}
        )


class ARunningOllama(_Machine):
    def test_suggests_a_chat_model_not_an_embedding_one(self) -> None:
        def answers(url: str):
            return {"models": [{"name": "nomic-embed-text:latest"}, {"name": "qwen3:8b"}]}

        ollama = self.found(probe=answers)[0]
        self.assertEqual((ollama.type, ollama.model), ("ollama", "qwen3:8b"))
        self.assertEqual(dict(ollama.connection), {"type": "ollama"})
        self.assertIn("1 model downloaded (qwen3:8b)", ollama.origin)

    def test_with_only_an_embedding_model_is_not_offered(self) -> None:
        def answers(url: str):
            return {"models": [{"name": "nomic-embed-text:latest"}]}

        self.assertEqual(self.found(probe=answers), [])


class ApiKeys(_Machine):
    def test_one_provider_per_key_set(self) -> None:
        self.environ.update({"ANTHROPIC_API_KEY": "sk-1", "OPENROUTER_API_KEY": "sk-2"})
        refs = {f.type: f.connection["auth"]["key"] for f in self.found()}
        self.assertEqual(
            refs, {"anthropic": "env:ANTHROPIC_API_KEY", "openrouter": "env:OPENROUTER_API_KEY"}
        )


if __name__ == "__main__":
    unittest.main()
