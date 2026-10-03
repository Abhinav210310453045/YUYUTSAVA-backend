"""Anthropic Claude — ``ChatAnthropic``.

Prompt caching rides on ``AnthropicPromptCachingMiddleware`` in the agent stack,
not here.
"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.providers.listing import get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

MODELS_URL = "https://api.anthropic.com/v1/models"


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

    def discover(self, access: ProviderAccess) -> Discovery:
        """Anthropic's ``/v1/models``: each model's window and output cap."""
        listing = get_json(
            MODELS_URL,
            label="Anthropic",
            headers={
                "x-api-key": access.secret(access.connection.auth.key),
                "anthropic-version": "2023-06-01",
            },
            params={"limit": 1000},
        )
        if listing.problem:
            return Discovery(access.provider, problem=listing.problem, fix=listing.fix)
        return Discovery(access.provider, models=parse_anthropic_models(listing.body))


def parse_anthropic_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """Anthropic's model list; every model it lists, the key can call."""
    return tuple(
        DiscoveredModel(
            id=entry["id"],
            label=entry.get("display_name") or "",
            context_window=entry.get("max_input_tokens"),
            max_output=entry.get("max_tokens"),
            tools=True,
            availability=Availability.AVAILABLE,
        )
        for entry in body.get("data") or []
    )


__all__ = ["AnthropicProvider", "parse_anthropic_models"]
