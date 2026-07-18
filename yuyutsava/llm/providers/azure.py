"""Azure OpenAI — ``AzureChatOpenAI``.

The only native-SDK provider with no ``require()`` guard: ``AzureChatOpenAI``
ships with ``langchain-openai``, which is already a core dependency.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import AzureOpenAISettings
from yuyutsava.llm.base import Provider


class AzureOpenAIProvider(Provider):
    settings_type = AzureOpenAISettings

    def build(
        self, settings: AzureOpenAISettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            api_key=SecretStr(settings.api_key),
            azure_endpoint=settings.azure_endpoint,
            azure_deployment=settings.azure_deployment,
            api_version=settings.api_version,
            temperature=temperature,
            max_tokens=4096,
        )


__all__ = ["AzureOpenAIProvider"]
