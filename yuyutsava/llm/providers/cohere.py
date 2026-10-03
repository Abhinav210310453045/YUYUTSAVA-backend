"""Cohere — ``ChatCohere``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.target import ModelTarget


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


__all__ = ["CohereProvider"]
