"""Azure OpenAI — ``AzureChatOpenAI``.

A model's ``id`` is the name of its deployment; ``base_model`` on its alias names
the model behind it, which is what usage rows record
(:meth:`~yuyutsava.llm.base.Provider.model_name`). ``AzureChatOpenAI`` is built
from the deployment alone and leaves its own ``model_name`` at ``None``, which is
why the name comes from the target, not the built model.

The only native-SDK provider with no ``require()`` guard: ``AzureChatOpenAI``
ships with ``langchain-openai``, which is already a core dependency.
"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider
from yuyutsava.llm.target import ModelTarget


class AzureOpenAIProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("azure",)

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        connection = target.connection
        return AzureChatOpenAI(
            api_key=SecretStr(target.secret(connection.auth.key)),
            azure_endpoint=connection.endpoint,
            azure_deployment=target.model,
            api_version=connection.api_version,
            temperature=options.temperature,
            max_tokens=ANSWER_TOKENS,
        )


__all__ = ["AzureOpenAIProvider"]
