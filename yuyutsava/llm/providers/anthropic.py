"""Anthropic Claude — ``ChatAnthropic``.

Prompt caching rides on ``AnthropicPromptCachingMiddleware`` in the agent stack,
not here.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import AnthropicSettings
from yuyutsava.llm.base import Provider, require


class AnthropicProvider(Provider):
    settings_type = AnthropicSettings
    key = "anthropic"

    def build(
        self, settings: AnthropicSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        # Not behind a yuyutsava extra — the install hint names the dist directly.
        mod = require(
            "langchain_anthropic", provider="anthropic", install="langchain-anthropic"
        )
        return mod.ChatAnthropic(
            api_key=SecretStr(settings.api_key),
            model_name=settings.model,
            temperature=temperature,
            max_tokens_to_sample=4096,
        )


__all__ = ["AnthropicProvider"]
