"""Which settings class an `LLM_PROVIDER` value selects.

W4 (OCP): `llm_settings_from_env` chose a provider with a twelve-branch `if`
chain, so adding a provider meant editing it, and the list of valid names in its
error message was written out separately — a second place to forget. These pin
the whole mapping first, so replacing the chain with a table is checked against
behaviour rather than against memory.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from yuyutsava.llm import settings as s

ALIASES = {
    "groq": s.GroqSettings,
    "openrouter": s.OpenRouterSettings,
    "ollama": s.OllamaSettings,
    "openai": s.OpenAISettings,
    "openai_compatible": s.OpenAICompatibleSettings,
    "custom": s.OpenAICompatibleSettings,
    "anthropic": s.AnthropicSettings,
    "google": s.GoogleSettings,
    "gemini": s.GoogleSettings,
    "vertex": s.VertexSettings,
    "bedrock": s.BedrockSettings,
    "aws": s.BedrockSettings,
    "azure": s.AzureOpenAISettings,
    "azure_openai": s.AzureOpenAISettings,
    "mistral": s.MistralSettings,
    "cohere": s.CohereSettings,
}


def _selected(env: dict, role=None) -> type:
    """The class chosen, without constructing it — `from_env` wants real keys."""
    chosen = []
    patches = [
        mock.patch.object(cls, "from_env", classmethod(lambda c, r=None: chosen.append(c)))
        for cls in set(ALIASES.values())
    ]
    with mock.patch.dict(os.environ, env, clear=True):
        for p in patches:
            p.start()
        try:
            s.llm_settings_from_env(role)
        finally:
            for p in patches:
                p.stop()
    return chosen[0]


class EveryAliasSelectsItsProvider(unittest.TestCase):
    def test_each_alias(self) -> None:
        for alias, cls in ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIs(_selected({"LLM_PROVIDER": alias}), cls)

    def test_case_is_ignored(self) -> None:
        self.assertIs(_selected({"LLM_PROVIDER": "AnThRoPiC"}), s.AnthropicSettings)

    def test_the_default_is_groq(self) -> None:
        self.assertIs(_selected({}), s.GroqSettings)


class Roles(unittest.TestCase):
    def test_a_role_prefix_wins(self) -> None:
        env = {"LLM_PROVIDER": "groq", "TRIAGE_LLM_PROVIDER": "anthropic"}
        self.assertIs(_selected(env, role="triage"), s.AnthropicSettings)

    def test_a_role_without_its_own_falls_back(self) -> None:
        self.assertIs(_selected({"LLM_PROVIDER": "mistral"}, role="triage"), s.MistralSettings)

    def test_main_means_no_role(self) -> None:
        env = {"LLM_PROVIDER": "cohere", "MAIN_LLM_PROVIDER": "anthropic"}
        self.assertIs(_selected(env, role="main"), s.CohereSettings)


class AnUnknownName(unittest.TestCase):
    def test_is_refused_with_every_valid_provider_named(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "nope"}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                s.llm_settings_from_env()
        self.assertEqual(
            str(caught.exception),
            "Unknown LLM_PROVIDER='nope'; use one of: groq, openrouter, ollama, openai, "
            "openai_compatible, anthropic, google, vertex, bedrock, azure, mistral, cohere.",
        )


class AddingAProviderIsOneRow(unittest.TestCase):
    """The point of the table: accepting a name and listing it in the error are
    the same fact, so they cannot disagree."""

    def test_a_new_row_is_accepted_and_named(self) -> None:
        class NewSettings:
            @classmethod
            def from_env(cls, role=None):
                return cls()

        table = {**s.SETTINGS_BY_PROVIDER, "newco": NewSettings}
        with mock.patch.object(s, "SETTINGS_BY_PROVIDER", table):
            with mock.patch.dict(os.environ, {"LLM_PROVIDER": "newco"}, clear=True):
                self.assertIsInstance(s.llm_settings_from_env(), NewSettings)
            with mock.patch.dict(os.environ, {"LLM_PROVIDER": "nope"}, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    s.llm_settings_from_env()
        self.assertIn("newco", str(caught.exception))

    def test_every_alias_the_tests_know_is_in_the_table(self) -> None:
        self.assertEqual(set(ALIASES), set(s.SETTINGS_BY_PROVIDER))


if __name__ == "__main__":
    unittest.main()
