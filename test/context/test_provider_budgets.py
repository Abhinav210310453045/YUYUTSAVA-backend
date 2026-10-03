"""Every accepted LLM_PROVIDER alias gets its model's real input window.

R-BUG-01. The compaction budget came from a table keyed by provider *string*
that knew seven names; every other alias fell to 128k. ``google`` compacted at
89.6k while its alias ``gemini`` got 1M, and ``bedrock`` (Claude, 200k) got 128k.
Compaction is the one step that drops messages from state, so an under-set
budget summarizes away history the model could still have held.

Now each provider type has its window (``llm.catalog``), found through the same
alias table ``LLM_PROVIDER`` resolves by.
"""

from __future__ import annotations

import unittest

from yuyutsava.context.config import default_max_input_tokens
from yuyutsava.llm.settings import PROVIDER_TYPES, provider_type_of

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

    def test_case_and_whitespace_do_not_matter_and_unknown_is_the_common_window(self) -> None:
        self.assertEqual(default_max_input_tokens("  Google "), 1_000_000)
        self.assertEqual(default_max_input_tokens("someday-provider"), 128_000)


if __name__ == "__main__":
    unittest.main()
