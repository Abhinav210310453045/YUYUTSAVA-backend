"""Every provider's "too long for the window" is recognised, and nothing else is."""

from __future__ import annotations

import unittest

from langchain_core.exceptions import ContextOverflowError

from yuyutsava.llm.overflow import is_context_overflow


class Recognised(unittest.TestCase):
    def test_the_sdks_own_error(self) -> None:
        self.assertTrue(is_context_overflow(ContextOverflowError("anything")))

    def test_the_providers_that_pass_their_own_through(self) -> None:
        for said in (
            # Claude on Vertex: Anthropic's Vertex client, unconverted.
            "Error code: 400 - {'type': 'invalid_request_error', "
            "'message': 'prompt is too long: 215000 tokens > 200000 maximum'}",
            # Bedrock Converse.
            "An error occurred (ValidationException) when calling the Converse operation: "
            "Input is too long for requested model.",
            # Mistral.
            "Prompt contains 140000 tokens, too large for model with 131072 maximum context length",
            # Gemini, when it arrives unconverted.
            "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576).",
        ):
            with self.subTest(said=said[:40]):
                self.assertTrue(is_context_overflow(RuntimeError(said)))


class NotAnOverflow(unittest.TestCase):
    def test_a_rate_limit_however_it_is_worded(self) -> None:
        said = (
            "Error code: 429 - Rate limit reached for model llama-3.3-70b on tokens per minute "
            "(TPM): Limit 12000; the request exceeds the maximum context length you may send "
            "per minute"
        )
        self.assertFalse(is_context_overflow(RuntimeError(said)))

    def test_anything_else(self) -> None:
        self.assertFalse(is_context_overflow(ValueError("tool schema is invalid")))


if __name__ == "__main__":
    unittest.main()
