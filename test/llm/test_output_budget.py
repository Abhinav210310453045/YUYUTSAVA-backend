"""What each provider lets the model write: the answer's share, and Gemini's thinking on top.

`R-BUG-23`. Gemini's thinking tokens count against ``max_output_tokens``. With
one 4,096 cap for both, a long think over a large context (a tool result into a
50k-125k-token history) used the whole cap and the reply came back empty — no
text, no tool call — and at our low temperature the retry thought the same way,
so a conversation stuck once it started. Thinking now has a budget of its own
and the cap is the answer's share plus that budget; the answer's share is one
constant, doubled, for every provider.

Each provider is built against a fake SDK that records its constructor kwargs,
so this checks what actually reaches the SDK. Gemini on Vertex and on AI Studio
share one builder (``providers/gemini.py``), so both are checked.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions
from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.schema import DEFAULT_THINKING_BUDGET
from yuyutsava.llm.config.secrets import Secret
from yuyutsava.llm.providers import (
    anthropic,
    azure,
    bedrock,
    cohere,
    gemini,
    google,
    mistral,
    openai_compat,
    vertex,
)
from yuyutsava.llm.settings import llm_settings_from_env
from yuyutsava.llm.target import ModelTarget, target_for

_CONNECTIONS = {
    "anthropic": 'type = "anthropic"\nauth = { method = "api_key", key = "secret:k" }',
    "azure": 'type = "azure"\nendpoint = "https://e"\nauth = { method = "api_key", key = "secret:k" }',
    "bedrock": 'type = "bedrock"\nregion = "us-east-1"\nauth = { method = "aws_default_chain" }',
    "cohere": 'type = "cohere"\nauth = { method = "api_key", key = "secret:k" }',
    "mistral": 'type = "mistral"\nauth = { method = "api_key", key = "secret:k" }',
    "openai_compatible": (
        'type = "openai_compatible"\nbase_url = "https://h/v1"\nauth = { method = "api_key", key = "secret:k" }'
    ),
    "vertex": 'type = "vertex"\nproject = "p"\nlocation = "global"\nauth = { method = "adc" }',
    "google": 'type = "google"\nauth = { method = "api_key", key = "secret:k" }',
}


def make_target(provider_type: str, model: str = "m", thinking: int | None = None) -> ModelTarget:
    """A target for one *provider_type* model, through the models config."""
    alias = f'[models.it]\nprovider = "p"\nid = "{model}"\n'
    if thinking is not None:
        alias += f"thinking = {{ budget = {thinking} }}\n"
    text = f'version = 1\n[defaults]\nmodel = "it"\n[providers.p]\n{_CONNECTIONS[provider_type]}\n{alias}'
    return target_for(load_text(text), "it", secrets={"secret:k": Secret("k")}, unavailable={})


class _Recorder:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _AnySdk:
    """Every SDK class (and the Gemini tool converter) is the recorder."""

    def __getattr__(self, name: str) -> type:
        return _Recorder


def _build(module, provider, target: ModelTarget) -> dict[str, Any]:
    patches = []
    for owner in (module, gemini):
        if hasattr(owner, "require"):
            patches.append(mock.patch.object(owner, "require", return_value=_AnySdk()))
    for attr in ("AzureChatOpenAI", "ChatOpenAI"):
        if hasattr(module, attr):
            patches.append(mock.patch.object(module, attr, _Recorder))
    # The login is not what this test is about: no credentials, no clients.
    patches.append(mock.patch.object(vertex, "google_credentials", return_value=object()))
    patches.append(mock.patch.object(bedrock, "aws_clients", return_value=(object(), object())))
    for patch in patches:
        patch.start()
    try:
        return provider.build(target, BuildOptions()).kwargs
    finally:
        for patch in patches:
            patch.stop()


class TheAnswerHasItsOwnShare(unittest.TestCase):
    def test_it_is_doubled_to_8192(self) -> None:
        self.assertEqual(ANSWER_TOKENS, 8192)

    def test_every_non_gemini_provider_caps_the_answer_at_that_share(self) -> None:
        cases = [
            (anthropic, anthropic.AnthropicProvider(), "anthropic", "max_tokens_to_sample"),
            (azure, azure.AzureOpenAIProvider(), "azure", "max_tokens"),
            (bedrock, bedrock.BedrockProvider(), "bedrock", "max_tokens"),
            (cohere, cohere.CohereProvider(), "cohere", "max_tokens"),
            (mistral, mistral.MistralProvider(), "mistral", "max_tokens"),
            (
                openai_compat,
                openai_compat.OpenAICompatibleProvider(),
                "openai_compatible",
                "max_completion_tokens",
            ),
        ]
        for module, provider, provider_type, key in cases:
            with self.subTest(provider=type(provider).__name__):
                self.assertEqual(
                    _build(module, provider, make_target(provider_type))[key], ANSWER_TOKENS
                )

    def test_claude_on_vertex_gets_the_answer_share_not_the_sdk_default(self) -> None:
        kwargs = _build(vertex, vertex.VertexProvider(), make_target("vertex", "claude-sonnet-4-6"))
        self.assertEqual(kwargs["max_output_tokens"], ANSWER_TOKENS)


def budget(target: ModelTarget) -> int:
    return gemini.thinking_budget(target)


class GeminiThinksOnItsOwnBudget(unittest.TestCase):
    def _gemini(self):
        return [
            (vertex, vertex.VertexProvider(), "vertex"),
            (google, google.GoogleProvider(), "google"),
        ]

    def test_a_thinking_model_gets_its_budget_and_the_answer_on_top(self) -> None:
        for module, provider, provider_type in self._gemini():
            for model in (
                "gemini-3.5-flash",
                "gemini-2.5-pro",
                "publishers/google/models/gemini-3.8-flash",
            ):
                with self.subTest(provider=type(provider).__name__, model=model):
                    kw = _build(module, provider, make_target(provider_type, model, thinking=5000))
                    self.assertEqual(kw["thinking_budget"], 5000)
                    self.assertEqual(kw["max_output_tokens"], ANSWER_TOKENS + 5000)

    def test_an_older_model_that_cannot_think_gets_no_thinking_config(self) -> None:
        for module, provider, provider_type in self._gemini():
            with self.subTest(provider=type(provider).__name__):
                kw = _build(module, provider, make_target(provider_type, "gemini-2.0-flash"))
                self.assertNotIn("thinking_budget", kw)
                self.assertEqual(kw["max_output_tokens"], ANSWER_TOKENS)

    def test_the_budget_defaults_to_8192_and_is_read_from_the_environment(self) -> None:
        self.assertEqual(DEFAULT_THINKING_BUDGET, 8192)
        env = {"VERTEX_PROJECT": "p", "GOOGLE_API_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("VERTEX_THINKING_BUDGET", "GOOGLE_THINKING_BUDGET"):
                os.environ.pop(var, None)
            for provider_type in ("vertex", "google"):
                os.environ["LLM_PROVIDER"] = provider_type
                self.assertEqual(budget(llm_settings_from_env()), DEFAULT_THINKING_BUDGET)
        with mock.patch.dict(
            os.environ, {**env, "VERTEX_THINKING_BUDGET": "2048", "GOOGLE_THINKING_BUDGET": "-5"}
        ):
            os.environ["LLM_PROVIDER"] = "vertex"
            self.assertEqual(budget(llm_settings_from_env()), 2048)
            os.environ["LLM_PROVIDER"] = "google"
            self.assertEqual(budget(llm_settings_from_env()), 0)  # never negative


if __name__ == "__main__":
    unittest.main()
