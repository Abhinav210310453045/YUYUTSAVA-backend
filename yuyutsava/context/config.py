"""Tunable knobs for the context controller.

Role-aware via the same ``<ROLE>_<NAME>`` env prefix mechanism the LLM
settings use (:func:`yuyutsava.config.env.env`), so e.g. the orchestrator
can compact earlier than the CLI chat agent::

    YUYUTSAVA_CONTEXT_COMPACT_FRACTION=0.7
    ORCHESTRATOR_YUYUTSAVA_CONTEXT_COMPACT_FRACTION=0.5
"""

from __future__ import annotations

from dataclasses import dataclass

from yuyutsava.config.env import env, env_flag, env_float, env_int
from yuyutsava.llm.settings import SETTINGS_BY_PROVIDER

# The budget the compactor steers under is the provider's real input window,
# declared by each provider's settings class (``max_input_tokens``) and found
# through the same alias table ``LLM_PROVIDER`` resolves by — so ``google`` and
# ``gemini`` cannot disagree. Erring low is not free: compaction is the one
# step that drops messages from state, so an under-set budget summarizes away
# history the model could still have held verbatim; ``compact_fraction``
# provides the headroom instead. An unknown provider name gets the common
# window.
_DEFAULT_MAX_INPUT_TOKENS = 128_000


def default_max_input_tokens(provider: str) -> int:
    """The input-context budget for an ``LLM_PROVIDER`` name (any accepted alias)."""
    settings_cls = SETTINGS_BY_PROVIDER.get(provider.strip().lower())
    if settings_cls is None:
        return _DEFAULT_MAX_INPUT_TOKENS
    return settings_cls.max_input_tokens


@dataclass(frozen=True)
class ContextSettings:
    """Compaction + offload thresholds for one agent role."""

    # Input-context budget the compactor steers under.
    max_input_tokens: int = _DEFAULT_MAX_INPUT_TOKENS

    # Compact when estimated tokens exceed this fraction of the budget.
    compact_fraction: float = 0.7

    # Messages preserved verbatim after a compaction (the recent tail).
    keep_messages: int = 20

    # Tool results larger than this are offloaded to the artifact store.
    offload_threshold_chars: int = 20_000

    # Tool-name prefixes whose results are *always* offloaded regardless of
    # size — reference-class tools (web search, bulk reads) whose raw payload is
    # never worth keeping inline. The compact digest stays in context; the full
    # body is fetched on demand via ctx_fetch_artifact/ctx_grep_artifact/ctx_recall.
    always_offload_prefixes: tuple[str, ...] = ("ws_",)

    # Index offloaded artifacts into the pgvector artifact_chunks table so the
    # agent can ctx_recall relevant slices. Postgres-only; a no-op on SQLite.
    semantic_recall: bool = True

    # Leading Human/System messages never summarized away (the task).
    pin_first_messages: int = 2

    # Token cap on the *summarizer's* input (the messages being condensed).
    summarizer_input_tokens: int = 12_000

    @classmethod
    def from_env(cls, role: str | None = None, *, provider: str = "") -> ContextSettings:
        def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = env(name, role)
            if not raw:
                return default
            parts = tuple(p.strip() for p in raw.split(",") if p.strip())
            return parts or default

        return cls(
            max_input_tokens=env_int(
                "YUYUTSAVA_CONTEXT_MAX_INPUT_TOKENS", role, default_max_input_tokens(provider)
            ),
            compact_fraction=env_float("YUYUTSAVA_CONTEXT_COMPACT_FRACTION", role, 0.7),
            keep_messages=env_int("YUYUTSAVA_CONTEXT_KEEP_MESSAGES", role, 20),
            offload_threshold_chars=env_int(
                "YUYUTSAVA_CONTEXT_OFFLOAD_THRESHOLD_CHARS", role, 20_000
            ),
            always_offload_prefixes=_csv("YUYUTSAVA_CONTEXT_ALWAYS_OFFLOAD_PREFIXES", ("ws_",)),
            semantic_recall=env_flag(
                "YUYUTSAVA_CONTEXT_SEMANTIC_RECALL", role, True, true=("1", "true", "yes", "on")
            ),
            pin_first_messages=env_int("YUYUTSAVA_CONTEXT_PIN_FIRST_MESSAGES", role, 2),
            summarizer_input_tokens=env_int(
                "YUYUTSAVA_CONTEXT_SUMMARIZER_INPUT_TOKENS", role, 12_000
            ),
        )

    @property
    def compact_trigger_tokens(self) -> int:
        """Absolute token threshold at which compaction fires."""
        return max(1, int(self.max_input_tokens * self.compact_fraction))
