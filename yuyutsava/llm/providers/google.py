"""Google Gemini via the google-genai API — ``ChatGoogleGenerativeAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import GoogleSettings
from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.quirks.gemini_parts import parts_safe


class GoogleProvider(Provider):
    settings_type = GoogleSettings

    def build(
        self, settings: GoogleSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require(
            "langchain_google_genai", provider="google", install="'yuyutsava[google]'"
        )
        # Same Gemini wire format as Vertex, same zero-parts hazard — one quirk
        # module, applied by both providers.
        cls = parts_safe(mod.ChatGoogleGenerativeAI)
        return cls(
            api_key=SecretStr(settings.api_key),
            model=settings.model,
            temperature=temperature,
            max_output_tokens=4096,
        )


__all__ = ["GoogleProvider"]
