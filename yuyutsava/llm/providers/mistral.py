"""Mistral — ``ChatMistralAI``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.target import ModelTarget


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


__all__ = ["MistralProvider"]
