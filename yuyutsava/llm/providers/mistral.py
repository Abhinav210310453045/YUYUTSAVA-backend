"""Mistral — ``ChatMistralAI``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.providers.listing import get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

MODELS_URL = "https://api.mistral.ai/v1/models"


class MistralProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("mistral",)

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        mod = require("langchain_mistralai", provider="Mistral", install="'yuyutsava[mistral]'")
        return mod.ChatMistralAI(
            api_key=SecretStr(target.secret(target.connection.auth.key)),
            model_name=target.model,
            temperature=options.temperature,
            max_tokens=ANSWER_TOKENS,
        )

    def discover(self, access: ProviderAccess) -> Discovery:
        """Mistral's ``/v1/models``: the chat models, with window and tool support."""
        key = access.secret(access.connection.auth.key)
        listing = get_json(MODELS_URL, label="Mistral", headers={"Authorization": f"Bearer {key}"})
        if listing.problem:
            return Discovery(access.provider, problem=listing.problem, fix=listing.fix)
        return Discovery(access.provider, models=parse_mistral_models(listing.body))


def parse_mistral_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """Mistral's model list: the models that can chat."""
    models = []
    for entry in body.get("data") or []:
        capabilities = entry.get("capabilities") or {}
        if capabilities and not capabilities.get("completion_chat", False):
            continue
        models.append(
            DiscoveredModel(
                id=entry["id"],
                label=entry.get("name") or "",
                context_window=entry.get("max_context_length"),
                tools=capabilities.get("function_calling") if capabilities else None,
                availability=Availability.AVAILABLE,
            )
        )
    return tuple(models)


__all__ = ["MistralProvider", "parse_mistral_models"]
