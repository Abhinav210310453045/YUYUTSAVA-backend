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
    # Drop vector hits below this cosine similarity so weakly-related memories
    # don't get injected into the prompt as "RELEVANT MEMORY". Applies to the
    # vector path only; the keyword fallback (score 0.0) is unaffected.
    min_score: float = 0.3
    # Skip writing a memory whose cosine similarity to an existing same-kind
    # memory is >= this — suppresses near-duplicate summaries/outcomes piling
    # up. Set > 1.0 to disable (nothing reaches exact 1.0 in practice).
    dedup_threshold: float = 0.97
    # nomic-embed-text is trained for asymmetric retrieval: prepend
    # ``search_document:`` to stored text and ``search_query:`` to queries.
    # Harmful for models not trained on these prefixes (e.g. OpenAI), so it's
    # auto-enabled only for nomic and overridable via YUYUTSAVA_EMBED_PREFIXES.
    embed_use_prefixes: bool = True

    @classmethod
    def from_env(cls, *, default_enabled: bool = False) -> MemorySettings:
        raw = os.environ.get("YUYUTSAVA_MEMORY_ENABLED", "").strip().lower()
        enabled = raw in ("1", "true", "yes") if raw else default_enabled

        # Local Ollama is the default. A separate cloud override lets you point
        # at a hosted OpenAI-compatible /embeddings endpoint without disturbing
        # the local defaults — set YUYUTSAVA_EMBED_CLOUD_URL (+ _KEY) to switch.
        # NB: a cloud model with a non-768 dimension needs a vector-column
        # migration (see storage/pg/migrations.py).
        cloud_url = os.environ.get("YUYUTSAVA_EMBED_CLOUD_URL", "").strip()
        if cloud_url:
            base = cloud_url.rstrip("/")
            key = (
                os.environ.get("YUYUTSAVA_EMBED_CLOUD_KEY", "").strip()
                or _env("EMBED_API_KEY", None, "ollama")
            )
        else:
            base = _env("EMBED_BASE_URL", None, OLLAMA_BASE_URL).rstrip("/")
            key = _env("EMBED_API_KEY", None, "ollama")
        if not base.endswith("/v1"):
            base = base + "/v1"
        model = os.environ.get("YUYUTSAVA_EMBED_MODEL", "").strip() or "nomic-embed-text"

        raw_k = os.environ.get("YUYUTSAVA_MEMORY_TOP_K", "").strip()
        try:
            top_k = int(raw_k) if raw_k else 5
        except ValueError:
            top_k = 5

        raw_pfx = os.environ.get("YUYUTSAVA_EMBED_PREFIXES", "").strip().lower()
        use_prefixes = (
            raw_pfx in ("1", "true", "yes") if raw_pfx else "nomic" in model.lower()
        )

        raw_min = os.environ.get("YUYUTSAVA_MEMORY_MIN_SCORE", "").strip()
        try:
            min_score = float(raw_min) if raw_min else 0.3
        except ValueError:
            min_score = 0.3

        raw_dedup = os.environ.get("YUYUTSAVA_MEMORY_DEDUP", "").strip()
        try:
            dedup_threshold = float(raw_dedup) if raw_dedup else 0.97
        except ValueError:
            dedup_threshold = 0.97

        return cls(
            enabled=enabled,
            embed_base_url=base,
            embed_api_key=key,
            embed_model=model,
            top_k=top_k,
            embed_use_prefixes=use_prefixes,
            min_score=min_score,
            dedup_threshold=dedup_threshold,
        )
