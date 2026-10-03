"""Anthropic Claude — ``ChatAnthropic``.

Prompt caching rides on ``AnthropicPromptCachingMiddleware`` in the agent stack,
not here.
"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.target import ModelTarget


class AnthropicProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("anthropic",)

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        # Not behind a yuyutsava extra — the install hint names the dist directly.
        mod = require("langchain_anthropic", provider="Anthropic", install="langchain-anthropic")
        return mod.ChatAnthropic(
            api_key=SecretStr(target.secret(target.connection.auth.key)),
            model_name=target.model,
            temperature=options.temperature,
            max_tokens_to_sample=ANSWER_TOKENS,
        )


__all__ = ["AnthropicProvider"]
