"""Mistral — ``ChatMistralAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import MistralSettings
from yuyutsava.llm.base import Provider, require


class MistralProvider(Provider):
    settings_type = MistralSettings

    def build(
        self, settings: MistralSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require(
            "langchain_mistralai", provider="mistral", install="'yuyutsava[mistral]'"
        )
        return mod.ChatMistralAI(
            api_key=SecretStr(settings.api_key),
            model_name=settings.model,
            temperature=temperature,
            max_tokens=4096,
        )


__all__ = ["MistralProvider"]
