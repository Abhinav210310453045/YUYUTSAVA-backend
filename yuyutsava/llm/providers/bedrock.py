"""AWS Bedrock — ``ChatBedrockConverse``. Auth is the boto3 credential chain."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import ANSWER_TOKENS, Provider, require
from yuyutsava.llm.settings import BedrockSettings


class BedrockProvider(Provider):
    settings_type = BedrockSettings
    key = "bedrock"

    def build(
        self, settings: BedrockSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require("langchain_aws", provider="bedrock", install="'yuyutsava[bedrock]'")
        return mod.ChatBedrockConverse(
            model=settings.model,
            region_name=settings.region,
            temperature=temperature,
            max_tokens=ANSWER_TOKENS,
        )


__all__ = ["BedrockProvider"]
