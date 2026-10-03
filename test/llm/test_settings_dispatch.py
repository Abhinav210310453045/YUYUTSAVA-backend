"""Which provider an ``LLM_PROVIDER`` value selects, and what the environment builds for it.

Until the environment cutover (design M7), processes still configure their
model through ``LLM_PROVIDER`` and its provider's variables; ``llm.settings``
turns those into a :class:`~yuyutsava.llm.target.ModelTarget`. These pin that the
selection, the role prefixes, the defaults and the messages are what they have
always been, and that every provider type the config schema knows can be chosen.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from yuyutsava.llm.config.schema import connection_types
from yuyutsava.llm.settings import PROVIDER_TYPES, llm_settings_from_env, provider_type_of

#: Every accepted LLM_PROVIDER spelling and the provider type it selects.
ALIASES = {
    "groq": "groq",
    "openrouter": "openrouter",
    "ollama": "ollama",
    "openai": "openai",
    "openai_compatible": "openai_compatible",
    "custom": "openai_compatible",
    "anthropic": "anthropic",
    "google": "google",
    "gemini": "google",
    "vertex": "vertex",
    "bedrock": "bedrock",
    "aws": "bedrock",
    "azure": "azure",
    "azure_openai": "azure",
    "mistral": "mistral",
    "cohere": "cohere",
}

#: Enough of each provider's variables for it to be chosen; the values are fakes.
KEYS = {
    "GROQ_API_KEY": "k",
    "OPENROUTER_API_KEY": "k",
    "OPENAI_API_KEY": "k",
    "OPENAI_COMPATIBLE_BASE_URL": "https://api.example.com/v1/",
    "OPENAI_COMPATIBLE_API_KEY": "k",
    "OPENAI_COMPATIBLE_MODEL": "some/model",
    "ANTHROPIC_API_KEY": "k",
    "GOOGLE_API_KEY": "k",
    "VERTEX_PROJECT": "p",
    "AZURE_OPENAI_API_KEY": "k",
    "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "dep",
    "MISTRAL_API_KEY": "k",
    "COHERE_API_KEY": "k",
}


def selected(env: dict[str, str], role: str | None = None) -> str:
    """The provider type the environment selects."""
    with mock.patch.dict(os.environ, {**KEYS, **env}, clear=True):
        return llm_settings_from_env(role).type


class EveryAliasSelectsItsProvider(unittest.TestCase):
    def test_each_alias(self) -> None:
        for alias, provider_type in ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(selected({"LLM_PROVIDER": alias}), provider_type)

    def test_case_is_ignored(self) -> None:
        self.assertEqual(selected({"LLM_PROVIDER": "AnThRoPiC"}), "anthropic")

    def test_the_default_is_groq(self) -> None:
        self.assertEqual(selected({}), "groq")

    def test_every_type_the_config_knows_can_be_chosen(self) -> None:
        self.assertEqual(sorted(PROVIDER_TYPES), sorted(connection_types()))
        self.assertEqual({provider_type_of(a) for a in ALIASES}, set(PROVIDER_TYPES))


class Roles(unittest.TestCase):
    def test_a_role_prefix_wins(self) -> None:
        env = {"LLM_PROVIDER": "groq", "TRIAGE_LLM_PROVIDER": "anthropic"}
        self.assertEqual(selected(env, role="triage"), "anthropic")

    def test_a_role_without_its_own_falls_back(self) -> None:
        self.assertEqual(selected({"LLM_PROVIDER": "mistral"}, role="triage"), "mistral")

    def test_main_means_no_role(self) -> None:
        env = {"LLM_PROVIDER": "cohere", "MAIN_LLM_PROVIDER": "anthropic"}
        self.assertEqual(selected(env, role="main"), "cohere")

    def test_a_role_key_is_used_before_the_shared_one(self) -> None:
        env = {"LLM_PROVIDER": "groq", "TRIAGE_GROQ_API_KEY": "role-key", "GROQ_API_KEY": "shared"}
        with mock.patch.dict(os.environ, env, clear=True):
            target = llm_settings_from_env("triage")
        self.assertEqual(target.connection.auth.key, "env:TRIAGE_GROQ_API_KEY")
        self.assertEqual(target.secret("env:TRIAGE_GROQ_API_KEY"), "role-key")


class WhatTheEnvironmentBuilds(unittest.TestCase):
    def test_defaults_are_unchanged(self) -> None:
        expected = {
            "groq": "llama-3.3-70b-versatile",
            "openrouter": "openai/gpt-4o-mini",
            "ollama": "gemma4:e2b",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-haiku-4-5-20251001",
            "google": "gemini-2.5-flash",
            "vertex": "gemini-2.5-flash",
            "bedrock": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "mistral": "mistral-large-latest",
            "cohere": "command-r-plus",
        }
        for provider_type, model in expected.items():
            with self.subTest(provider=provider_type):
                with mock.patch.dict(
                    os.environ, {**KEYS, "LLM_PROVIDER": provider_type}, clear=True
                ):
                    self.assertEqual(llm_settings_from_env().model, model)

    def test_the_key_is_a_reference_not_a_copy(self) -> None:
        with mock.patch.dict(
            os.environ, {**KEYS, "LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1"}, clear=True
        ):
            target = llm_settings_from_env()
        self.assertEqual(target.connection.auth.key, "env:GROQ_API_KEY")
        self.assertNotIn("gsk-1", repr(target))

    def test_a_missing_key_says_which_variable_to_set(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "groq"}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                llm_settings_from_env()
        self.assertTrue(str(caught.exception).startswith("Set GROQ_API_KEY in the environment"))

    def test_an_azure_deployment_keeps_the_model_behind_it(self) -> None:
        env = {**KEYS, "LLM_PROVIDER": "azure", "AZURE_OPENAI_MODEL": "gpt-4o"}
        with mock.patch.dict(os.environ, env, clear=True):
            target = llm_settings_from_env()
        self.assertEqual((target.model, target.entry.base_model), ("dep", "gpt-4o"))


class AnUnknownName(unittest.TestCase):
    def test_is_refused_with_every_valid_provider_named(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "nope"}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                llm_settings_from_env()
        self.assertEqual(
            str(caught.exception),
            "Unknown LLM_PROVIDER='nope'; use one of: groq, openrouter, ollama, openai, "
            "openai_compatible, anthropic, google, vertex, bedrock, azure, mistral, cohere.",
        )


if __name__ == "__main__":
    unittest.main()
