"""Google Gemini on Vertex AI — ``ChatVertexAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from yuyutsava.core.config import VertexSettings
from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.quirks.gemini_parts import parts_safe


class VertexProvider(Provider):
    settings_type = VertexSettings

    def build(
        self, settings: VertexSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require(
            "langchain_google_vertexai", provider="vertex", install="'yuyutsava[vertex]'"
        )
        # Gemini 400s the whole request if any message renders to zero parts,
        # which permanently wedges a checkpointed thread. See quirks/gemini_parts.
        cls = parts_safe(mod.ChatVertexAI)
        return cls(
            model=settings.model,
            project=settings.project,
            location=settings.location,
            temperature=temperature,
            max_output_tokens=4096,
        )


__all__ = ["VertexProvider"]
