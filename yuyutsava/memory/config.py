"""Tunable knobs for semantic memory.

The embedder is configured through the same role-prefix mechanism as the
chat models, under the role ``embed``::

    EMBED_LLM_PROVIDER=ollama          # default — local, free
    EMBED_OLLAMA_MODEL is NOT used; the embed model has its own var:
    YUYUTSAVA_EMBED_MODEL=nomic-embed-text

Any OpenAI-compatible ``/embeddings`` endpoint works (Ollama, OpenRouter,
OpenAI proper via base-url override).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from yuyutsava.core.config import OLLAMA_BASE_URL, _env

# Must match the vector(768) column in storage/pg/migrations.py.
EMBEDDING_DIM = 768


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = False
    embed_base_url: str = OLLAMA_BASE_URL
    embed_api_key: str = "ollama"
    embed_model: str = "nomic-embed-text"
    top_k: int = 5

    @classmethod
    def from_env(cls, *, default_enabled: bool = False) -> MemorySettings:
        raw = os.environ.get("YUYUTSAVA_MEMORY_ENABLED", "").strip().lower()
        enabled = raw in ("1", "true", "yes") if raw else default_enabled

        base = _env("EMBED_BASE_URL", None, OLLAMA_BASE_URL).rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        key = _env("EMBED_API_KEY", None, "ollama")
        model = os.environ.get("YUYUTSAVA_EMBED_MODEL", "").strip() or "nomic-embed-text"

        raw_k = os.environ.get("YUYUTSAVA_MEMORY_TOP_K", "").strip()
        try:
            top_k = int(raw_k) if raw_k else 5
        except ValueError:
            top_k = 5

        return cls(
            enabled=enabled,
            embed_base_url=base,
            embed_api_key=key,
            embed_model=model,
            top_k=top_k,
        )
