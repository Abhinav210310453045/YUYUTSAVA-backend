"""Cohere — ``ChatCohere``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import CohereSettings
from yuyutsava.llm.base import Provider, require


class CohereProvider(Provider):
    settings_type = CohereSettings

    def build(
        self, settings: CohereSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require("langchain_cohere", provider="cohere", install="'yuyutsava[cohere]'")
        return mod.ChatCohere(
            cohere_api_key=SecretStr(settings.api_key),
            model=settings.model,
            temperature=temperature,
            max_tokens=4096,
        )


__all__ = ["CohereProvider"]
