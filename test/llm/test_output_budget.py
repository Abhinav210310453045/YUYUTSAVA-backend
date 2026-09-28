"""What each provider lets the model write: the answer's share, and Gemini's thinking on top.

`R-BUG-23`. Gemini's thinking tokens count against ``max_output_tokens``. With
one 4,096 cap for both, a long think over a large context (a tool result into a
50k-125k-token history) used the whole cap and the reply came back empty — no
text, no tool call — and at our low temperature the retry thought the same way,
so a conversation stuck once it started. Thinking now has a budget of its own
and the cap is the answer's share plus that budget; the answer's share is one
constant, doubled, for every provider.

Each provider is built against a fake SDK that records its constructor kwargs,
so this checks what actually reaches the SDK.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from yuyutsava.llm.base import ANSWER_TOKENS
from yuyutsava.llm.providers import (
    anthropic,
    azure,
    bedrock,
    cohere,
    google,
    mistral,
    openai_compat,
    vertex,
)
from yuyutsava.llm.settings import (
    DEFAULT_THINKING_BUDGET,
    AnthropicSettings,
    AzureOpenAISettings,
    BedrockSettings,
    CohereSettings,
    GoogleSettings,
    MistralSettings,
    OpenAICompatibleSettings,
    VertexSettings,
)


class _Recorder:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _AnySdk:
    """Every SDK class is the recorder; Vertex's schema check is the identity."""

    functions_utils = type("F", (), {"_format_to_gapic_tool": staticmethod(lambda t: t)})

    def __getattr__(self, name: str) -> type:
        return _Recorder


def _build(module, provider, settings) -> dict[str, Any]:
    patches = (
        [mock.patch.object(module, "require", return_value=_AnySdk())]
        if hasattr(module, "require")
        else []
    )
    for attr in ("AzureChatOpenAI", "ChatOpenAI"):
        if hasattr(module, attr):
            patches.append(mock.patch.object(module, attr, _Recorder))
    for p in patches:
        p.start()
    try:
        return provider.build(settings, temperature=0.1, disable_reasoning=False).kwargs
    finally:
        for p in patches:
            p.stop()


class TheAnswerHasItsOwnShare(unittest.TestCase):
    def test_it_is_doubled_to_8192(self) -> None:
        self.assertEqual(ANSWER_TOKENS, 8192)

    def test_every_non_gemini_provider_caps_the_answer_at_that_share(self) -> None:
        cases = [
            (
                anthropic,
                anthropic.AnthropicProvider(),
                AnthropicSettings(api_key="k"),
                "max_tokens_to_sample",
            ),
            (
                azure,
                azure.AzureOpenAIProvider(),
                AzureOpenAISettings(api_key="k", azure_endpoint="https://e", azure_deployment="d"),
                "max_tokens",
            ),
            (bedrock, bedrock.BedrockProvider(), BedrockSettings(region="us-east-1"), "max_tokens"),
            (cohere, cohere.CohereProvider(), CohereSettings(api_key="k"), "max_tokens"),
            (mistral, mistral.MistralProvider(), MistralSettings(api_key="k"), "max_tokens"),
            (
                openai_compat,
                openai_compat.OpenAICompatibleProvider(),
                OpenAICompatibleSettings(api_key="k", base_url="https://h/v1", model="m"),
                "max_completion_tokens",
            ),
        ]
        for module, provider, settings, key in cases:
            with self.subTest(provider=type(provider).__name__):
                self.assertEqual(_build(module, provider, settings)[key], ANSWER_TOKENS)


class GeminiThinksOnItsOwnBudget(unittest.TestCase):
    def _gemini(self):
        return [
            (vertex, vertex.VertexProvider(), lambda **kw: VertexSettings(project="p", **kw)),
            (google, google.GoogleProvider(), lambda **kw: GoogleSettings(api_key="k", **kw)),
        ]

    def test_a_thinking_model_gets_its_budget_and_the_answer_on_top(self) -> None:
        for module, provider, make in self._gemini():
            for model in (
                "gemini-3.5-flash",
                "gemini-2.5-pro",
                "publishers/google/models/gemini-3.8-flash",
            ):
                with self.subTest(provider=type(provider).__name__, model=model):
                    kw = _build(module, provider, make(model=model, thinking_budget=5000))
                    self.assertEqual(kw["thinking_budget"], 5000)
                    self.assertEqual(kw["max_output_tokens"], ANSWER_TOKENS + 5000)

    def test_an_older_model_that_cannot_think_gets_no_thinking_config(self) -> None:
        for module, provider, make in self._gemini():
            with self.subTest(provider=type(provider).__name__):
                kw = _build(module, provider, make(model="gemini-2.0-flash"))
                self.assertNotIn("thinking_budget", kw)
                self.assertEqual(kw["max_output_tokens"], ANSWER_TOKENS)

    def test_the_budget_defaults_to_8192_and_is_read_from_the_environment(self) -> None:
        self.assertEqual(DEFAULT_THINKING_BUDGET, 8192)
        env = {"VERTEX_PROJECT": "p", "GOOGLE_API_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("VERTEX_THINKING_BUDGET", "GOOGLE_THINKING_BUDGET"):
                os.environ.pop(var, None)
            self.assertEqual(VertexSettings.from_env().thinking_budget, DEFAULT_THINKING_BUDGET)
            self.assertEqual(GoogleSettings.from_env().thinking_budget, DEFAULT_THINKING_BUDGET)
        with mock.patch.dict(
            os.environ, {**env, "VERTEX_THINKING_BUDGET": "2048", "GOOGLE_THINKING_BUDGET": "-5"}
        ):
            self.assertEqual(VertexSettings.from_env().thinking_budget, 2048)
            self.assertEqual(GoogleSettings.from_env().thinking_budget, 0)  # never negative


if __name__ == "__main__":
    unittest.main()
