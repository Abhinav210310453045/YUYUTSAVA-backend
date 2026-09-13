"""Google Gemini on Vertex AI — ``ChatVertexAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from yuyutsava.core.config import VertexSettings
from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.quirks.first_chunk_retry import first_chunk_retry
from yuyutsava.llm.quirks.gemini_parts import parts_safe
from yuyutsava.llm.quirks.loop_affinity import loop_pinned


class VertexProvider(Provider):
    settings_type = VertexSettings
    key = "vertex"
    # The grpc.aio client binds to the loop that first drives it — declared here
    # so a builder can ask, rather than learn from loop_pinned's error.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(
        self, settings: VertexSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require(
            "langchain_google_vertexai", provider="vertex", install="'yuyutsava[vertex]'"
        )
        # Gemini 400s the whole request if any message renders to zero parts,
        # which permanently wedges a checkpointed thread. See quirks/gemini_parts.
        # The SDK's retry decorator covers only the stream *open*; a 429/503 that
        # lands on the first read escapes it and kills the turn. See
        # quirks/first_chunk_retry.
        # The grpc.aio client binds to the first event loop that uses it; see
        # quirks/loop_affinity for the one-instance-per-loop rule it enforces.
        cls = loop_pinned(first_chunk_retry(parts_safe(mod.ChatVertexAI)))
        return cls(
            model=settings.model,
            project=settings.project,
            location=settings.location,
            temperature=temperature,
            max_output_tokens=4096,
            # ONE retry ladder, not two nested ones. The SDK's decorator wraps
            # the stream *open* and ours covers the first read, so with both at
            # 6 a quota storm became 6x6 attempts: minutes of silence per call.
            # The quirk owns the policy (it is the layer that sees the error
            # that actually kills turns, and it has the wall-clock budget), so
            # the SDK keeps only a token retry for non-streaming calls.
            # VERTEX_MAX_RETRIES still steers the quirk via settings.
            max_retries=min(2, settings.max_retries),
        )


__all__ = ["VertexProvider"]
