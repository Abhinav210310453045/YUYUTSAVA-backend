"""Tunable knobs for the context controller.

Role-aware via the same ``<ROLE>_<NAME>`` env prefix mechanism the LLM
settings use (:func:`yuyutsava.core.config._env`), so e.g. the orchestrator
can compact earlier than the CLI chat agent::

    YUYUTSAVA_CONTEXT_COMPACT_FRACTION=0.7
    ORCHESTRATOR_YUYUTSAVA_CONTEXT_COMPACT_FRACTION=0.5
"""

from __future__ import annotations

from dataclasses import dataclass

from yuyutsava.core.config import _env

# Conservative per-provider input-context defaults. These are *budgets* the
# compactor steers under, not API hard limits — erring low just compacts a
# little earlier.
_PROVIDER_MAX_INPUT_TOKENS: dict[str, int] = {
    "anthropic": 200_000,
    "groq": 128_000,
    "openrouter": 128_000,
    "ollama": 8_192,
}
_DEFAULT_MAX_INPUT_TOKENS = 128_000


def default_max_input_tokens(provider: str) -> int:
    """Best-effort input-context budget for a provider name."""
    return _PROVIDER_MAX_INPUT_TOKENS.get(provider.strip().lower(), _DEFAULT_MAX_INPUT_TOKENS)


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
        def _int(name: str, default: int) -> int:
            raw = _env(name, role)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = _env(name, role)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = _env(name, role)
            if not raw:
                return default
            parts = tuple(p.strip() for p in raw.split(",") if p.strip())
            return parts or default

        def _bool(name: str, default: bool) -> bool:
            raw = _env(name, role)
            if raw is None or raw == "":
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            max_input_tokens=_int(
                "YUYUTSAVA_CONTEXT_MAX_INPUT_TOKENS",
                default_max_input_tokens(provider),
            ),
            compact_fraction=_float("YUYUTSAVA_CONTEXT_COMPACT_FRACTION", 0.7),
            keep_messages=_int("YUYUTSAVA_CONTEXT_KEEP_MESSAGES", 20),
            offload_threshold_chars=_int(
                "YUYUTSAVA_CONTEXT_OFFLOAD_THRESHOLD_CHARS", 20_000
            ),
            always_offload_prefixes=_csv(
                "YUYUTSAVA_CONTEXT_ALWAYS_OFFLOAD_PREFIXES", ("ws_",)
            ),
            semantic_recall=_bool("YUYUTSAVA_CONTEXT_SEMANTIC_RECALL", True),
            pin_first_messages=_int("YUYUTSAVA_CONTEXT_PIN_FIRST_MESSAGES", 2),
            summarizer_input_tokens=_int(
                "YUYUTSAVA_CONTEXT_SUMMARIZER_INPUT_TOKENS", 12_000
            ),
        )

    @property
    def compact_trigger_tokens(self) -> int:
        """Absolute token threshold at which compaction fires."""
        return max(1, int(self.max_input_tokens * self.compact_fraction))
