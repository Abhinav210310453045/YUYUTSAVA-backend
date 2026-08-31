"""Google Gemini via the google-genai API — ``ChatGoogleGenerativeAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.core.config import GoogleSettings
from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.quirks.gemini_parts import parts_safe
from yuyutsava.llm.quirks.loop_affinity import loop_pinned


class GoogleProvider(Provider):
    settings_type = GoogleSettings
    key = "google"
    # Same loop-bound async client as Vertex — see quirks/loop_affinity.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(
        self, settings: GoogleSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require(
            "langchain_google_genai", provider="google", install="'yuyutsava[google]'"
        )
        # Same Gemini wire format as Vertex, same zero-parts hazard — one quirk
        # module, applied by both providers. Same loop-bound async client too —
        # see quirks/loop_affinity.
        cls = loop_pinned(parts_safe(mod.ChatGoogleGenerativeAI))
        return cls(
            api_key=SecretStr(settings.api_key),
            model=settings.model,
            temperature=temperature,
            max_output_tokens=4096,
        )


__all__ = ["GoogleProvider"]
