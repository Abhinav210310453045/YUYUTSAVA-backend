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
    key = "azure"

    def model_name(self, settings: AzureOpenAISettings) -> str:
        """Fall back to the deployment, which is what actually served the call.

        Azure is why :class:`~yuyutsava.llm.handle.ModelHandle` takes the name
        from *settings* rather than from the built model: ``AzureChatOpenAI`` is
        constructed with ``azure_deployment`` and never given ``model``, so it
        leaves ``model_name`` at ``None`` and the old attribute probe returned
        ``""`` for every Azure model — silently blanking the model column on
        Azure usage rows. Reading settings, which the base implementation
        already does, is what fixes that.

        This override covers the remaining hole: ``settings.model`` is purely
        informational here and may be empty, in which case the deployment name
        is the only true identifier available.
        """
        return settings.model or settings.azure_deployment

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
