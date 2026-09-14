"""One token counter, or the panel and the compactor tell different stories.

The compaction trigger is computed from langchain's ``count_tokens_approximately``
with per-model tuning. If the context panel used its own estimator, a user could
watch "7 % used" while history was being summarized away — so these pin that
:mod:`yuyutsava.context.tokens` defers to the same function with the same
parameters the compactor picks, and that nothing in it can raise.

Run:  .venv/bin/python test/context/test_tokens.py
"""

from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.context.tokens import (
    ANTHROPIC_CHARS_PER_TOKEN,
    DEFAULT_CHARS_PER_TOKEN,
    approx_tokens,
    approx_tokens_chars,
    approx_tokens_text,
    chars_per_token_for,
)


class _FakeModel:
    def __init__(self, llm_type: str) -> None:
        self._llm_type = llm_type


class PerModelTuning(unittest.TestCase):
    def test_anthropic_uses_the_denser_ratio(self):
        self.assertEqual(
            chars_per_token_for(_FakeModel("anthropic-chat")),
            ANTHROPIC_CHARS_PER_TOKEN,
        )

    def test_everything_else_uses_the_default(self):
        for llm_type in ("chat-google-generativeai", "groq-chat", "openai-chat", ""):
            self.assertEqual(
                chars_per_token_for(_FakeModel(llm_type)), DEFAULT_CHARS_PER_TOKEN
            )

    def test_no_model_uses_the_default(self):
        self.assertEqual(chars_per_token_for(None), DEFAULT_CHARS_PER_TOKEN)

    def test_it_matches_the_ratio_langchain_picks_for_the_compactor(self):
        # The whole point: our tuning is not an independent guess. If langchain
        # changes its figure, this fails rather than the panel silently drifting
        # away from the compaction trigger.
        from langchain.agents.middleware.summarization import (
            _get_approximate_token_counter,
        )

        counter = _get_approximate_token_counter(_FakeModel("anthropic-chat"))
        self.assertEqual(
            counter.keywords["chars_per_token"], ANTHROPIC_CHARS_PER_TOKEN
        )
        generic = _get_approximate_token_counter(_FakeModel("groq-chat"))
        self.assertNotIn("chars_per_token", generic.keywords)


class Counting(unittest.TestCase):
    def test_a_message_list_counts_more_than_its_bare_text(self):
        # Role, name and per-message overhead are real prompt bytes.
        msgs = [SystemMessage(content="a" * 400), HumanMessage(content="b" * 400)]
        self.assertGreater(approx_tokens(msgs), approx_tokens_text("a" * 800))

    def test_text_and_char_forms_agree(self):
        self.assertEqual(approx_tokens_text("x" * 4_000), approx_tokens_chars(4_000))

    def test_a_slice_has_no_per_message_overhead(self):
        # Slices of one message must sum to the whole, so they cannot each
        # carry a per-message penalty.
        self.assertEqual(
            approx_tokens_text("x" * 2_000) + approx_tokens_text("y" * 2_000),
            approx_tokens_text("z" * 4_000),
        )

    def test_empty_input_is_zero_not_one(self):
        self.assertEqual(approx_tokens_text(""), 0)
        self.assertEqual(approx_tokens_chars(0), 0)
        self.assertEqual(approx_tokens_chars(-5), 0)
        self.assertEqual(approx_tokens([]), 0)

    def test_a_tiny_slice_still_costs_a_token(self):
        self.assertEqual(approx_tokens_text("x"), 1)

    def test_denser_tokenization_yields_more_tokens_for_the_same_text(self):
        text = "x" * 3_300
        anthropic = approx_tokens_text(text, model=_FakeModel("anthropic-chat"))
        generic = approx_tokens_text(text, model=_FakeModel("groq-chat"))
        self.assertGreater(anthropic, generic)


class NeverRaises(unittest.TestCase):
    def test_garbage_messages_count_zero_rather_than_exploding(self):
        # A telemetry number is never worth a failed turn.
        self.assertEqual(approx_tokens(object()), 0)

    def test_a_model_without_an_llm_type_is_fine(self):
        self.assertEqual(chars_per_token_for(object()), DEFAULT_CHARS_PER_TOKEN)

    def test_a_non_string_llm_type_is_fine(self):
        self.assertEqual(chars_per_token_for(_FakeModel(None)),
                         DEFAULT_CHARS_PER_TOKEN)

    def test_messages_with_odd_content_still_count(self):
        msgs = [AIMessage(content=[{"type": "text", "text": "hi"}])]
        self.assertGreaterEqual(approx_tokens(msgs), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
