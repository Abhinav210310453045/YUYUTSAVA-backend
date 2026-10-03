"""Google Gemini on Vertex AI — ``ChatVertexAI``."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.gemini_budget import gemini_output_kwargs
from yuyutsava.llm.quirks.first_chunk_retry import first_chunk_retry
from yuyutsava.llm.quirks.gemini_parts import parts_safe
from yuyutsava.llm.quirks.gemini_tool_schema import tool_schema_safe
from yuyutsava.llm.quirks.loop_affinity import loop_pinned
from yuyutsava.llm.settings import VertexSettings


class VertexProvider(Provider):
    settings_type = VertexSettings
    key = "vertex"
    # The grpc.aio client binds to the loop that first drives it — declared here
    # so a builder can ask, rather than learn from loop_pinned's error.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(
        self, settings: VertexSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        mod = require("langchain_google_vertexai", provider="vertex", install="'yuyutsava[vertex]'")
        # Gemini 400s the whole request if any message renders to zero parts,
        # which permanently wedges a checkpointed thread. See quirks/gemini_parts.
        # The SDK's retry decorator covers only the stream *open*; a 429/503 that
        # lands on the first read escapes it and kills the turn. See
        # quirks/first_chunk_retry.
        # The grpc.aio client binds to the first event loop that uses it; see
        # quirks/loop_affinity for the one-instance-per-loop rule it enforces.
        # The SDK's schema converter rejects a JSON Schema type list, which MCP
        # servers publish, and one such tool failed every turn. The quirk rewrites
        # type lists, and uses the SDK's own converter to leave out any tool Vertex
        # still cannot express. See quirks/gemini_tool_schema.
        check = mod.functions_utils._format_to_gapic_tool
        cls = loop_pinned(first_chunk_retry(parts_safe(tool_schema_safe(mod.ChatVertexAI, check))))
        return cls(
            model=settings.model,
            project=settings.project,
            location=settings.location,
            temperature=temperature,
            **gemini_output_kwargs(settings.model, settings.thinking_budget),
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
