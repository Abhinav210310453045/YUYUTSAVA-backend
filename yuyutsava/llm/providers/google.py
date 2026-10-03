"""Gemini through Google AI Studio — an API key, ``ChatGoogleGenerativeAI``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import BuildOptions, Provider
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.gemini import build_gemini
from yuyutsava.llm.target import ModelTarget


class GoogleProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("google",)
    # genai caches an async client on the loop that first drives it — see quirks/loop_affinity.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        key = target.secret(target.connection.auth.key)
        return build_gemini(target, options, api_key=SecretStr(key))


__all__ = ["GoogleProvider"]
