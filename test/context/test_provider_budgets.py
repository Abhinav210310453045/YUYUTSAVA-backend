"""Every accepted LLM_PROVIDER alias gets its model's real input window.

R-BUG-01. The compaction budget came from a table keyed by provider *string*
that knew seven names; every other alias fell to 128k. ``google`` compacted at
89.6k while its alias ``gemini`` got 1M, and ``bedrock`` (Claude, 200k) got 128k.
Compaction is the one step that drops messages from state, so an under-set
budget summarizes away history the model could still have held.

Now the window comes from the model a task actually runs on (``llm.catalog``);
on the environment route, that model's provider type is found through the same
alias table ``LLM_PROVIDER`` resolves by, so ``google`` and ``gemini`` cannot
disagree. These pin the window each alias's default model gets *before* its
SDK profile is consulted, which is what the catalog falls back to.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from yuyutsava.llm.catalog import context_window
from yuyutsava.llm.settings import PROVIDER_TYPES, llm_settings_from_env, provider_type_of

#: Enough of each provider's variables for it to be chosen; the values are fakes.
_KEYS = {
    "GROQ_API_KEY": "k",
    "OPENROUTER_API_KEY": "k",
    "OPENAI_API_KEY": "k",
    "OPENAI_COMPATIBLE_BASE_URL": "https://api.example.com/v1",
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


def default_max_input_tokens(alias: str) -> int:
    """The window the environment route's model for *alias* gets, before its profile."""
    with mock.patch.dict(os.environ, {**_KEYS, "LLM_PROVIDER": alias}, clear=True):
        return context_window(llm_settings_from_env())


EXPECTED = {
    "groq": 128_000,
    "openrouter": 128_000,
    "ollama": 8_192,
    "openai": 128_000,
    "openai_compatible": 128_000,
    "custom": 128_000,
    "anthropic": 200_000,
    "google": 1_000_000,
    "gemini": 1_000_000,
    "vertex": 1_000_000,
    "bedrock": 200_000,
    "aws": 200_000,
    "azure": 128_000,
    "azure_openai": 128_000,
    "mistral": 128_000,
    "cohere": 128_000,
}


class EveryAliasHasItsWindow(unittest.TestCase):
    def test_each_accepted_alias(self) -> None:
        for alias, window in EXPECTED.items():
            with self.subTest(alias=alias):
                self.assertEqual(default_max_input_tokens(alias), window)

    def test_every_provider_type_is_covered(self) -> None:
        """A new provider type must come with a decision about its window."""
        self.assertTrue(set(PROVIDER_TYPES) <= set(EXPECTED))
        self.assertTrue(all(provider_type_of(alias) for alias in EXPECTED))

    def test_aliases_of_one_provider_agree(self) -> None:
        by_type: dict[str, set[int]] = {}
        for alias in EXPECTED:
            by_type.setdefault(provider_type_of(alias), set()).add(default_max_input_tokens(alias))
        for provider_type, windows in by_type.items():
            with self.subTest(provider=provider_type):
                self.assertEqual(len(windows), 1)

    def test_case_does_not_matter(self) -> None:
        self.assertEqual(default_max_input_tokens("Google"), 1_000_000)


if __name__ == "__main__":
    unittest.main()
