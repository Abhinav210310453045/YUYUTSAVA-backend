"""Cohere — ``ChatCohere``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.providers.listing import get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

MODELS_URL = "https://api.cohere.com/v1/models"


class CohereProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("cohere",)

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        mod = require("langchain_cohere", provider="Cohere", install="'yuyutsava[cohere]'")
        return mod.ChatCohere(
            cohere_api_key=SecretStr(target.secret(target.connection.auth.key)),
            model=target.model,
            temperature=options.temperature,
            max_tokens=ANSWER_TOKENS,
        )

    def discover(self, access: ProviderAccess) -> Discovery:
        """Cohere's ``/v1/models``, restricted to models served on the chat endpoint."""
        key = access.secret(access.connection.auth.key)
        listing = get_json(
            MODELS_URL,
            label="Cohere",
            headers={"Authorization": f"Bearer {key}"},
            params={"endpoint": "chat", "page_size": 1000},
        )
        if listing.problem:
            return Discovery(access.provider, problem=listing.problem, fix=listing.fix)
        return Discovery(access.provider, models=parse_cohere_models(listing.body))


def parse_cohere_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """Cohere's chat models, with window and tool support when listed."""
    return tuple(
        DiscoveredModel(
            id=entry["name"],
            context_window=entry.get("context_length"),
            tools=("tools" in entry["features"]) if entry.get("features") is not None else None,
            availability=Availability.AVAILABLE,
        )
        for entry in body.get("models") or []
    )


__all__ = ["CohereProvider", "parse_cohere_models"]
